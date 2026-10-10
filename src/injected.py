"""Penulis DB 9router — transaksi aman, app tidak perlu dihentikan."""

from __future__ import annotations

import sqlite3
import uuid

from .util import now_iso

# Whitelist resmi 9router (app/api/proxy-pools/route.js): nilai di luar
# daftar ini dilempar paksa menjadi "http" oleh server.
TYPES = {"http", "vercel", "cloudflare", "deno", "netlify"}


def _db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60, isolation_level=None)
    con.execute("PRAGMA busy_timeout=60000")
    return con


def existing_urls(con) -> set[str]:
    import json

    out = set()
    for (data,) in con.execute("SELECT data FROM proxyPools"):
        try:
            out.add(json.loads(data).get("proxyUrl"))
        except Exception:
            continue
    return out


def _iso(ts: "datetime.datetime") -> str:
    """Stempel milidetik tepat. Dipakai sebagai kunci urutan unik, jadi
    harus benar-benar berpresisi — isoformat(timespec='milliseconds')
    membulatkan dan membuat dua baris berbagi stempel yang sama."""
    base = ts.strftime("%Y-%m-%dT%H:%M:%S")
    ms = f"{ts.microsecond // 1000:03d}"
    return f"{base}.{ms}Z"


def push_custom_on_top(con) -> tuple[int, int]:
    """Pindahkan semua custom (type=http) ke urutan teratas.

    9router mengurutkan proxyPools berdasarkan updatedAt menurun. Kalau
    semua baris diberi timestamp yang sama, urutan di antara mereka tidak
    deterministik — jadi tiap baris diberi stempel unik yang mundur satu
    milidetik.

    Baseline-nya bukan jam dinding saat ini (rawan salah zona waktu), tapi
    updatedAt tertinggi yang sudah ada di DB. Dengan begitu blok custom
    selalu tampil di atas vercel/cloudflare berapa pun zona waktu mesin.
    """
    import datetime
    import json

    rows = con.execute(
        "SELECT id, data FROM proxyPools WHERE json_extract(data,'$.type')='http'"
    ).fetchall()
    if not rows:
        return 0, 0

    top = con.execute("SELECT MAX(updatedAt) FROM proxyPools").fetchone()[0]

    def parse(ts: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))

    base = parse(top) + datetime.timedelta(milliseconds=1) if top else datetime.datetime.now(
        datetime.timezone.utc
    )

    for i, (pid, _data) in enumerate(rows):
        ts = (base - datetime.timedelta(milliseconds=i))
        con.execute("UPDATE proxyPools SET updatedAt=? WHERE id=?", (_iso(ts), pid))
    return len(rows), len(rows)


def set_order(con, groups: list[list[str]]) -> int:
    """Susun ulang blok berdasarkan daftar indeks baris.

    `groups` adalah daftar blok, tiap blok berisi id baris dalam urutan
    yang diinginkan; blok pertama paling atas (9router urutkan updatedAt
    menurun). Setiap id mendapat stempel unik yang mundur.
    """
    import datetime

    con.execute(
        "UPDATE proxyPools SET updatedAt=? WHERE updatedAt > ?",
        ("2000-01-01T00:00:00.001Z", "2000-01-01T00:00:00.001Z"),
    )
    top = con.execute("SELECT MAX(updatedAt) FROM proxyPools").fetchone()[0]
    base = (
        datetime.datetime.fromisoformat(top.replace("Z", "+00:00"))
        if top
        else datetime.datetime.now(datetime.timezone.utc)
    )
    n = 0
    for group in groups:
        for j, pid in enumerate(group):
            ts = (base - datetime.timedelta(milliseconds=n))
            con.execute("UPDATE proxyPools SET updatedAt=? WHERE id=?", (_iso(ts), pid))
            n += 1
    return n


def inject(con, rows: list[dict]) -> dict:
    """Tulis/segarkan kandidat. Satu transaksi; semua atau tidak sama sekali."""
    import json

    now = now_iso()
    cur = con.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        known = existing_urls(con)
        inserted = updated = skipped = 0

        for r in rows:
            url = r["proxyUrl"]
            if url in known:
                skipped += 1
                continue
            payload = {
                "name": f"farm-{r['protocol']}-{r['host']}:{r['port']}",
                "proxyUrl": url,
                "noProxy": "",
                "type": "http",  # wajar untuk proxy custom; socks5 disimpan di metadata
                "strictProxy": False,
                "lastTestedAt": r.get("lastTestedAt"),
                "lastError": r.get("lastError"),
                "importedAt": now,
                "importedProto": r["protocol"],
                "importedSource": r.get("source"),
                "latencyMs": r.get("latencyMs"),
            }
            cur.execute(
                "INSERT INTO proxyPools(id, isActive, testStatus, data, createdAt, updatedAt)"
                " VALUES(?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    1 if r.get("alive") else 0,
                    "active" if r.get("alive") else "error",
                    json.dumps(payload),
                    now,
                    now,
                ),
            )
            inserted += 1

        con.execute("COMMIT")
        return {"inserted": inserted, "updated": updated, "skipped": skipped}
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise


def update_existing(con, patches: dict[str, dict]) -> int:
    """Segarkan status baris yang sudah ada (testStatus/lastError/lastTestedAt)."""
    import json

    now = now_iso()
    cur = con.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        n = 0
        for pid, p in patches.items():
            data = cur.execute(
                "SELECT data FROM proxyPools WHERE id=?", (pid,)
            ).fetchone()
            if not data:
                continue
            d = json.loads(data[0])
            if "testStatus" in p:
                d["lastTestedAt"] = now
            d.update(p)
            if p.get("alive") is False:
                d["lastError"] = p.get("reason", "farm: gagal uji")
            elif p.get("alive") is True:
                d.pop("lastError", None)
            cur.execute(
                "UPDATE proxyPools SET data=json(?), testStatus=COALESCE(?, testStatus),"
                " updatedAt=? WHERE id=?",
                (
                    json.dumps(d),
                    "active" if p.get("alive") else "error",
                    now,
                    pid,
                ),
            )
            n += 1
        con.execute("COMMIT")
        return n
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
