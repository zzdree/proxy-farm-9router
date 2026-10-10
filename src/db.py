"""Penulis DB 9router — dengan rangkap keamanan.

Aturan main (dari config.toml [scope]):
  * vercel & cloudflare  → read-only, tak pernah disentuh
  * type=http            → boleh recheck / update / prune
  * prune hanya milik tool (baris bertanda importedSource)
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import uuid

# Whitelist resmi 9router: nilai di luar daftar ini dilempar server jadi "http".
TYPES = {"http", "vercel", "cloudflare", "deno", "netlify"}

# Baris provisioning milik tool (di luar "source") yang juga melindungi dirinya.
OWNED_MARK = "importedSource"


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60, isolation_level=None)
    con.execute("PRAGMA busy_timeout=60000")
    return con


def row_view(con) -> list[dict]:
    """Ambil baris proxyPools sebagai dict ter-normalisasi."""
    rows = con.execute(
        "SELECT id, isActive, testStatus, data, updatedAt FROM proxyPools"
    ).fetchall()
    out = []
    for pid, active, status, data, updated in rows:
        try:
            d = json.loads(data)
        except Exception:
            continue
        out.append(
            {
                "id": pid,
                "isActive": bool(active),
                "testStatus": status,
                "type": d.get("type"),
                "proxyUrl": d.get("proxyUrl"),
                "name": d.get("name"),
                "owned": bool(d.get(OWNED_MARK)),
                "data": d,
                "updatedAt": updated,
            }
        )
    return out


def bound_connection_ids(con) -> set[str]:
    """Kumpulkan id proxy yang terikat koneksi provider aktif.

    Dipakai sebagai penjaga: 9router akan menolak hapus baris yang sedang
    dipakai (HTTP 409). Kalau ada baris seperti itu, kita skip hapus dan
    cukup tandai error.
    """
    import json as _json

    bound = set()
    for (data,) in con.execute("SELECT data FROM providerConnections WHERE data IS NOT NULL"):
        try:
            d = _json.loads(data)
        except Exception:
            continue
        psd = d.get("providerSpecificData") or {}
        ppid = psd.get("proxyPoolId")
        if ppid:
            bound.add(ppid)
    return bound


def _iso(ts: datetime.datetime) -> str:
    """Stempel milidetik presisi (dipakai sebagai kunci urutan unik)."""
    return f"{ts.strftime('%Y-%m-%dT%H:%M:%S')}.{ts.microsecond // 1000:03d}Z"


def order_rows(con, groups: list[str], top_id: str | None = None) -> int:
    """Susun ulang sesuai urutan blok di `groups`; blok pertama paling atas.

    9router mengurutkan proxyPools berdasarkan updatedAt menurun. Tiap baris
    diberi stempel unik yang mundur 1 ms supaya urutannya deterministik.
    Baseline-nya diambil dari updatedAt teratas yang sudah ada (bukan jam
    dinding — celah zona waktu pernah merusak urutan sekali, jangan diulang).
    """
    rows = row_view(con)
    if not rows:
        return 0

    by_type: dict[str, list[dict]] = {g: [] for g in groups}
    for r in rows:                      # type di luar groups dibiarkan
        by_type.setdefault(r["type"], [])
    for r in rows:
        if r["type"] in by_type:
            by_type[r["type"]].append(r)

    top = con.execute("SELECT MAX(updatedAt) FROM proxyPools").fetchone()[0]
    base = (
        datetime.datetime.fromisoformat(top.replace("Z", "+00:00"))
        if top
        else datetime.datetime.now(datetime.timezone.utc)
    )
    n = 0
    for g in groups:
        # internal blok: urutkan by name biar deterministik
        for r in sorted(by_type[g], key=lambda x: x["name"] or ""):
            ts = (base - datetime.timedelta(milliseconds=n))
            con.execute(
                "UPDATE proxyPools SET updatedAt=? WHERE id=?", (_iso(ts), r["id"])
            )
            n += 1
    return n


def prune_dead(con, scope: dict, local_ip_ok: bool = False) -> dict:
    """Hapus baris http milik tool yang strike-nya cukup.

    scope:
      prune_types   : list type yang boleh dipangkas
      only_prune_owned : hanya hapus baris bertanda importedSource
      grace_strikes : berapa strike sebelum dibuang
    """
    rows = row_view(con)
    bound = bound_connection_ids(con)
    pruned, skipped_bound, skipped_grace = [], [], []

    cur = con.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        for r in rows:
            if r["type"] not in scope["prune_types"]:
                continue
            if scope.get("only_prune_owned") and not r["owned"]:
                continue
            if r["id"] in bound:
                skipped_bound.append(r)
                continue
            d = r["data"]
            strikes = int(d.get("strikes") or 0)
            # mati pada run ini DITANDAI sementara oleh caller (strikes sudah +1)?
            if strikes < scope["grace_strikes"]:
                skipped_grace.append(r)
                continue
            cur.execute("DELETE FROM proxyPools WHERE id=?", (r["id"],))
            pruned.append({"id": r["id"], "name": r["name"], "url": r["proxyUrl"]})
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "pruned": pruned,
        "skipped_bound": [r["name"] for r in skipped_bound],
        "skipped_grace": len(skipped_grace),
    }


def mark_result(con, pid: str, verdict, *, is_new_row: bool, now: str) -> dict:
    """Tulis hasil uji ke baris: testStatus, lastError, lastTestedAt, strikes."""
    d = json.loads(con.execute("SELECT data FROM proxyPools WHERE id=?", (pid,)).fetchone()[0])
    d["lastTestedAt"] = now
    if verdict.alive:
        d.pop("lastError", None)
        d["strikes"] = 0
        if verdict.latency_ms is not None:
            d["latencyMs"] = verdict.latency_ms
        if verdict.exit_ip:
            d["exitIp"] = verdict.exit_ip
        status = "active"
        active = 1
    else:
        d["strikes"] = int(d.get("strikes") or 0) + 1
        d["lastError"] = verdict.reason or "farm: gagal uji"
        status = "error"
        active = 0
    con.execute(
        "UPDATE proxyPools SET isActive=?, testStatus=?, data=json(?) WHERE id=?",
        (active, status, json.dumps(d), pid),
    )
    return {"strikes": d["strikes"], "status": status}


def insert_new(con, cand, verdict, now: str) -> None:
    """Masukkan proxy baru yang lolos uji."""
    payload = {
        "name": f"farm-{cand.protocol}-{cand.host}:{cand.port}",
        "proxyUrl": cand.proxy_url,
        "noProxy": "",
        "type": "http",
        "strictProxy": False,
        "importedSource": cand.source,
        "importedProto": cand.protocol,
        "importedAt": now,
        "lastTestedAt": now,
        "strikes": 0,
    }
    if verdict.latency_ms is not None:
        payload["latencyMs"] = verdict.latency_ms
    con.execute(
        "INSERT INTO proxyPools(id, isActive, testStatus, data, createdAt, updatedAt)"
        " VALUES(?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            1,
            "active",
            json.dumps(payload),
            now,
            now,
        ),
    )
