#!/usr/bin/env python3
"""zzproxy-farm — auto farm proxy publik, uji, dan sunting ke DB 9router.

Alur: fetch (8 sumber) -> dedup -> uji -> filter -> inject -> laporan.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import tomllib
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import fetch as fetch_mod
from src import injected as dbmod
from src import report as report_mod
from src import testers

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "config.toml")
SRC_PATH = os.path.join(ROOT, "sources.json")


def load_config():
    with open(CFG_PATH, "rb") as f:
        return tomllib.load(f)


def backup_db(db_path: str, backup_dir: str, keep: int) -> str | None:
    """Salin DB (+ WAL) sebelum menulis. Cadangan tiap sesi, rotasi otomatis."""
    if not os.path.exists(db_path):
        return None
    dest_dir = os.path.join(ROOT, backup_dir)
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(dest_dir, f"pre-farm-{stamp}")
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(db_path, os.path.join(dest, "data.sqlite"))
    for ext in ("-wal", "-shm"):
        p = db_path + ext
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dest, f"data.sqlite{ext}"))
    old = sorted(
        d for d in os.listdir(dest_dir) if d.startswith("pre-farm-")
    )
    while len(old) > keep:
        shutil.rmtree(os.path.join(dest_dir, old.pop(0)), ignore_errors=True)
    return dest


def local_ip() -> str | None:
    """IP keluar mesin ini — dipakai untuk mendeteksi proxy yang membocorkan IP asli."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except OSError:
        return None


async def main_async(args) -> int:
    cfg = load_config()
    farm_cfg = cfg.get("farm", {})
    test_cfg = cfg.get("test", {})
    db_path = cfg.get("database", {}).get("path")
    if not db_path or not os.path.exists(db_path):
        print(f"DB tidak ditemukan: {db_path}", file=sys.stderr)
        return 2

    with open(SRC_PATH, encoding="utf-8") as f:
        sources = json.load(f)["sources"]
    sources = [s for s in sources if s.get("enabled", True)]
    if args.limit_sources:
        keep = set(args.limit_sources.split(","))
        sources = [s for s in sources if s["id"] in keep]
    if not sources:
        print("Tidak ada sumber aktif.", file=sys.stderr)
        return 2

    print(f"== zzproxy-farm == sumber aktif: {len(sources)} | mode uji: {test_cfg.get('mode','tcp')}")

    # 1) Fetch + dedup
    cands = await fetch_mod.gather(
        sources,
        max_candidates=farm_cfg.get("max_candidates", 8000),
        concurrency=farm_cfg.get("concurrency", 250),
    )
    if not cands:
        print("Tidak ada kandidat — sumber mati semua atau hasil kosong.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"\n[DRY-RUN] {len(cands)} kandidat terkumpul, tidak menulis apa pun.")
        for c in cands[: args.show]:
            print(f"   {c.proxy_url:42s} <- {c.source}")
        return 0

    # 2) Uji
    lip = local_ip()
    t0 = time.monotonic()
    results = await testers.test_all(
        cands,
        mode=args.mode or test_cfg.get("mode", "tcp"),
        timeout=farm_cfg.get("connect_timeout", 4.0),
        concurrency=farm_cfg.get("concurrency", 250),
        local_ip=lip,
    )
    elapsed = round(time.monotonic() - t0, 1)
    alive_pairs = [(c, r) for c, r in results if r.alive]

    # 3) Siapkan laporan sebelum menulis
    per_source, per_proto = {}, {}
    for c, r in results:
        s = per_source.setdefault(c.source, {"candidates": 0, "alive": 0, "dead": 0})
        s["candidates"] += 1
        s["alive" if r.alive else "dead"] += 1
        if r.alive:
            per_proto[c.protocol] = per_proto.get(c.protocol, 0) + 1
    fastest = sorted(
        (
            {
                "proxyUrl": c.proxy_url,
                "protocol": c.protocol,
                "latencyMs": r.latency_ms,
                "source": c.source,
            }
            for c, r in alive_pairs
            if r.latency_ms is not None
        ),
        key=lambda x: x["latencyMs"],
    )

    # 4) Inject (backup dulu)
    print("  [4] INJECT ke DB 9router")
    bkp = backup_db(db_path, cfg["database"].get("backup_dir", "backups"), cfg["database"].get("keep_backups", 10))
    if bkp:
        print(f"      cadangan dibuat: {bkp}")

    rows = [
        {
            "proxyUrl": c.proxy_url,
            "protocol": c.protocol,
            "host": c.host,
            "port": c.port,
            "alive": True,
            "source": c.source,
            "latencyMs": r.latency_ms,
            "lastTestedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        for c, r in alive_pairs
    ]

    con = dbmod._db(db_path)
    try:
        before = len(dbmod.existing_urls(con))
        stats = dbmod.inject(con, rows)
        if args.keep_custom_on_top:
            groups = [[r[0] for r in con.execute(
                "SELECT id FROM proxyPools WHERE json_extract(data,'$.type')=? ORDER BY id",
                (t,),
            ).fetchall()] for t in ("http", "vercel", "cloudflare")]
            moved = dbmod.set_order(con, groups)
            print(
                f"      {moved} baris disusun: custom (atas) -> vercel -> cloudflare (bawah)"
            )
        after = len(dbmod.existing_urls(con))
        integrity = con.execute("PRAGMA integrity_check(1)").fetchone()[0]
    finally:
        con.close()

    db_custom_total = after - before + stats["inserted"]
    summary = {
        "sources_total": len(sources),
        "candidates": len(cands),
        "unique": len(results),
        "alive": len(alive_pairs),
        "dead": len(results) - len(alive_pairs),
        "alive_pct": round(len(alive_pairs) * 100 / len(results), 1) if results else None,
        "inserted": stats["inserted"],
        "skipped": stats["skipped"],
        "updated": stats["updated"],
        "db_custom_total": stats["inserted"] + stats["skipped"],
        "elapsed_s": elapsed,
        "integrity": integrity,
        "backup": bkp,
    }
    stamp = report_mod.stamp_now()
    jpath, mpath = report_mod.write_reports(
        os.path.join(ROOT, "reports"),
        {"summary": summary, "per_source": per_source, "per_protocol": per_proto, "fastest": fastest},
        stamp,
    )
    # simpan juga daftar hidup untuk proses lanjut
    with open(os.path.join(ROOT, "data", f"alive-{stamp}.json"), "w") as f:
        json.dump([r["proxyUrl"] for r in rows], f)

    print("\n== RINGKASAN ==")
    for k, v in summary.items():
        if k != "backup":
            print(f"   {k:16s} {v}")
    print(f"\n   laporan : {mpath}")
    print(f"   data    : data/alive-{stamp}.json")
    return 0


def main():
    p = argparse.ArgumentParser(description="Auto farm proxy -> 9router DB")
    p.add_argument("--dry-run", action="store_true", help="hanya kumpulkan kandidat")
    p.add_argument("--show", type=int, default=15, help="jumlah contoh saat --dry-run")
    p.add_argument("--mode", choices=["tcp", "exitip"], default=None, help="mode uji")
    p.add_argument("--limit-sources", default=None, help="id sumber, dipisah koma")
    p.add_argument("--no-order", dest="keep_custom_on_top", action="store_false",
                   help="jangan pindahkan custom ke urutan teratas")
    args = p.parse_args()
    try:
        raise SystemExit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        print("\ndibatalkan", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
