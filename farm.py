#!/usr/bin/env python3
"""proxy-farm-9router — bot: farm proxy publik, recheck yang sudah ada, bersihkan yang
mati, dan suntik yang hidup ke DB 9router. Semua dalam satu klik.

Peringatan: proxy publik berarti operatornya bisa membaca lalu lintas yang
melewatinya. Jangan mengarahkannya ke request yang membawa API key provider
atau isi prompt skripsi.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# socksio dibutuhkan untuk uji SOCKS sungguhan (httpx tidak menerima scheme
# socks5 tanpanya). Dilepas di .pylibs supaya tak menyentuh Python sistem.
LOCAL_LIBS = os.path.join(HERE, ".pylibs")
if os.path.isdir(LOCAL_LIBS) and LOCAL_LIBS not in sys.path:
    sys.path.insert(0, LOCAL_LIBS)

import tomllib

from src import db as dbmod
from src import fetch as fetch_mod
from src import report as report_mod
from src import testers


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def human(n) -> str:
    return f"{n:,}".replace(",", ".")


def local_ip() -> str | None:
    """IP keluar mesin ini — dipakai mendeteksi proxy yang membocorkan IP asli."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def backup_db(db_path: str, backup_dir: str, keep: int) -> str | None:
    """Salin DB (+WAL) sebelum run. Rotasi: sisakan `keep` cadangan terakhir."""
    if not os.path.exists(db_path):
        return None
    dest_root = os.path.join(HERE, backup_dir)
    os.makedirs(dest_root, exist_ok=True)
    dest = os.path.join(dest_root, f"pre-run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}")
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(db_path, os.path.join(dest, "data.sqlite"))
    for ext in ("-wal", "-shm"):
        p = db_path + ext
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dest, f"data.sqlite{ext}"))
    old = sorted(d for d in os.listdir(dest_root) if d.startswith("pre-run-"))
    while len(old) > keep:
        shutil.rmtree(os.path.join(dest_root, old.pop(0)), ignore_errors=True)
    return dest


def parse_endpoint(url: str) -> tuple[str, int]:
    _, _, rest = url.partition("://")
    host, _, port = rest.rpartition(":")
    return host, int(port)


def progress(done: int, total: int, extra: str = "") -> None:
    if total <= 0:
        return
    filled = int(28 * done / total)
    bar = "█" * filled + "░" * (28 - filled)
    print(f"\r  {bar} {done:,}/{total:,} {extra}", end="", flush=True)
    if done >= total:
        print()


async def run(args) -> int:
    cfg = load_config(os.path.join(HERE, "config.toml"))
    db_path = cfg["database"]["path"]
    if not os.path.exists(db_path):
        print(f"DB tidak ditemukan: {db_path}", file=sys.stderr)
        return 2

    scope = cfg["scope"]
    farm_cfg = cfg["farm"]
    mode = args.mode or cfg.get("test", {}).get("mode", "deep")

    with open(os.path.join(HERE, "sources.json"), encoding="utf-8") as f:
        sources = json.load(f)["sources"]
    sources = [s for s in sources if s.get("enabled", True)]
    if args.limit_sources:
        keep = set(args.limit_sources.split(","))
        sources = [s for s in sources if s["id"] in keep]

    print("=" * 66)
    print(f"  proxy-farm-9router  |  {len(sources)} sumber  |  mode: {mode}")
    print(f"  aman: {', '.join(scope['recheck_types'])} boleh disentuh; "
          f"vercel/cloudflare READ-ONLY")
    print("=" * 66)

    con = dbmod.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    t_start = time.monotonic()
    lip = local_ip()

    try:
        # ── 1) backup + snapshot kondisi DB ───────────────────────────────
        bkp = backup_db(db_path, cfg["database"]["backup_dir"], cfg["database"]["keep_backups"])
        if bkp:
            print(f"\n  [0] cadangan DB -> {bkp}")

        before = dbmod.row_view(con)
        by_type_before: dict[str, int] = {}
        for r in before:
            by_type_before[r["type"]] = by_type_before.get(r["type"], 0) + 1
        print("  [0] kondisi DB sekarang: " +
              ", ".join(f"{k}={v}" for k, v in sorted(by_type_before.items())))

        # ── 2) fetch kandidat baru + dedup ────────────────────────────────
        cands = await fetch_mod.gather(
            sources, farm_cfg.get("max_candidates", 8000), farm_cfg.get("concurrency", 250)
        )
        if not cands:
            print("tidak ada kandidat — semua sumber mati?", file=sys.stderr)
            return 1

        # kandidat yang belum ada di DB (berdasarkan proxyUrl)
        known_urls = {r["proxyUrl"] for r in before if r["proxyUrl"]}
        fresh = [c for c in cands if c.proxy_url not in known_urls]
        print(f"  [2b] {len(fresh)} kandidat benar-benar baru (sisanya sudah ada di DB)")

        # ── 3) RECHECK baris lama (hanya type dalam scope.recheck_types) ──
        recheck_types = set(scope["recheck_types"])
        old_rows = [r for r in before if r["type"] in recheck_types]
        print(f"\n  [3] RECHECK {human(len(old_rows))} baris lama "
              f"(type={sorted(recheck_types)})")

        sem = asyncio.Semaphore(farm_cfg.get("concurrency", 250))
        tcp_to = farm_cfg.get("connect_timeout", 4.0)
        deep_to = farm_cfg.get("deep_test_timeout", 6.0)
        endpoints = farm_cfg.get(
            "exit_ip_endpoints", ["https://api.ipify.org", "https://icanhazip.com"]
        )
        deep_n = farm_cfg.get("deep_test_top_n", 800)

        async def probe_row(r):
            async with sem:
                try:
                    host, port = parse_endpoint(r["proxyUrl"])
                except Exception:
                    return r, testers.Verdict(False, reason="url_tak_terbaca")
                tcp = await testers.tcp_probe(host, port, tcp_to)
                if not tcp.alive:
                    return r, tcp
                return r, await testers.live_probe(r["proxyUrl"], endpoints, deep_to, lip)

        results = []
        done = 0
        total = len(old_rows)
        alive_old = 0
        for fut in asyncio.as_completed([probe_row(r) for r in old_rows]):
            row, verdict = await fut
            results.append((row, verdict))
            if verdict.alive:
                alive_old += 1
            done += 1
            if done % 250 == 0 or done == total:
                progress(done, total, f"hidup={alive_old}")
        dead_old = total - alive_old
        print(f"  [3b] recheck: {alive_old} hidup / {dead_old} mati")

        # tulis hasil recheck
        now = now_iso()
        con.execute("BEGIN IMMEDIATE")
        try:
            for row, verdict in results:
                dbmod.mark_result(con, row["id"], verdict, is_new_row=False, now=now)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        # ── 4) PRUNE (yang mati & strike cukup) ──────────────────────────
        prune_stats = {"pruned": [], "skipped_bound": [], "skipped_grace": 0}
        dead_striked = [r for r, v in results if not v.alive and r["owned"]]
        if args.no_prune:
            print(f"  [4] --no-prune: {len(dead_striked)} baris mati dibiarkan")
        elif not dead_striked:
            print("  [4] tak ada baris mati milik tool -> tak ada yang dipangkas")
        else:
            if args.dry_run:
                print(f"  [4] DRY-RUN: akan memangkas {len(dead_striked)} baris "
                      f"(strike >= {farm_cfg['grace_strikes']} di run ini)")
                for r in dead_striked[:10]:
                    print(f"        - {r['name']}")
            else:
                prune_stats = dbmod.prune_dead(
                    con,
                    {
                        "prune_types": scope["prune_types"],
                        "only_prune_owned": scope.get("only_prune_owned", True),
                        "grace_strikes": farm_cfg["grace_strikes"],
                    },
                )
                print(f"  [4] dipangkas      : {len(prune_stats['pruned'])}")
                print(f"      ditahan (grace): {prune_stats['skipped_grace']}")
                print(f"      dilewati (terikat koneksi): {len(prune_stats['skipped_bound'])}")

        # ── 5) INJECT kandidat baru (uji dulu, baru tulis) ───────────────
        print(f"\n  [5] UJI {human(len(fresh))} kandidat baru (tcp dulu, "
              f"exit-IP utk {min(len(fresh), deep_n)} tercepat)")

        async def probe_fresh(c):
            async with sem:
                host, port = c.host, c.port
                tcp = await testers.tcp_probe(host, port, tcp_to)
                if not tcp.alive:
                    return c, tcp
                return c, await testers.live_probe(c.proxy_url, endpoints, deep_to, lip)

        new_results = []
        done = 0
        total = len(fresh)
        alive_new = 0
        for fut in asyncio.as_completed([probe_fresh(c) for c in fresh]):
            cand, verdict = await fut
            new_results.append((cand, verdict))
            if verdict.alive:
                alive_new += 1
            done += 1
            if done % 250 == 0 or done == total:
                progress(done, total, f"hidup={alive_new}")
        print(f"  [5b] kandidat baru: {alive_new} hidup / {total - alive_new} mati")

        if args.dry_run:
            print("\n  [6] DRY-RUN: tidak menulis kandidat baru ke DB")
            inserted = 0
        else:
            con.execute("BEGIN IMMEDIATE")
            try:
                inserted = 0
                for cand, verdict in new_results:
                    if not verdict.alive:
                        continue
                    dbmod.insert_new(con, cand, verdict, now_iso())
                    inserted += 1
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            print(f"  [6] {inserted} proxy baru masuk ke DB")

        # ── 6) rangkap: vercel/cloudflare tak tersentuh ─────────────────
        after = dbmod.row_view(con)
        vc_before = {r["id"]: r["updatedAt"] for r in before if r["type"] in ("vercel", "cloudflare")}
        vc_after = {r["id"]: r["updatedAt"] for r in after if r["type"] in ("vercel", "cloudflare")}
        touched = [i for i, ts in vc_after.items() if vc_before.get(i) != ts]
        if touched:
            # Ini tak boleh terjadi. Lebih baik gagal keras daripada diam.
            raise RuntimeError(f"RANGKAP MELANGAR: {len(touched)} baris vercel/cloudflare berubah")

        # ── 7) rapikan urutan ──────────────────────────────────────────
        if args.do_order and not args.dry_run:
            con.execute("BEGIN IMMEDIATE")
            try:
                n = dbmod.order_rows(con, cfg["order"]["groups"])
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            print(f"  [8] {n} baris disusun: " + " -> ".join(cfg["order"]["groups"]))
        else:
            print("  [7] urutan dilewati (--dry-run / --no-order)")

        integrity = con.execute("PRAGMA integrity_check(1)").fetchone()[0]

        # ── 8) laporan ─────────────────────────────────────────────────
        final = dbmod.row_view(con)
        http_rows = [r for r in final if r["type"] == "http"]
        http_alive = sum(1 for r in http_rows if r["testStatus"] == "active")
        summary = {
            "mode": mode,
            "duration_s": round(time.monotonic() - t_start, 1),
            "sources": len(sources),
            "candidates": len(cands),
            "fresh": len(fresh),
            "rechecked": len(old_rows),
            "alive_old": alive_old,
            "dead_old": dead_old,
            "new_alive": alive_new,
            "inserted": inserted,
            "pruned": len(prune_stats["pruned"]),
            "held_grace": prune_stats["skipped_grace"],
            "skipped_bound": len(prune_stats["skipped_bound"]),
            "http_total": len(http_rows),
            "http_alive": http_alive,
            "db_total": len(final),
            "vercel": sum(1 for r in final if r["type"] == "vercel"),
            "cloudflare": sum(1 for r in final if r["type"] == "cloudflare"),
            "integrity": integrity,
            "guard_ok": True,
        }
        stamp = report_mod.stamp_now()
        per_source: dict[str, dict] = {}
        for c in cands:
            s = per_source.setdefault(c.source, {"candidates": 0})
            s["candidates"] += 1
        new_by_src = {}
        for c, v in new_results:
            s = new_by_src.setdefault(c.source, {"alive": 0, "dead": 0})
            s["alive" if v.alive else "dead"] += 1
        fastest = sorted(
            [
                {
                    "proxyUrl": c.proxy_url,
                    "protocol": c.protocol,
                    "latencyMs": v.latency_ms,
                    "source": c.source,
                }
                for c, v in new_results
                if v.alive and v.latency_ms is not None
            ],
            key=lambda x: x["latencyMs"],
        )
        # gabung hitungan per sumber: kandidat (dari fetch) + hidup/mati (dari uji baru)
        merged = {}
        for sid, v in per_source.items():
            merged[sid] = {"candidates": v["candidates"], "alive": 0, "dead": 0}
        for sid, v in new_by_src.items():
            m = merged.setdefault(sid, {"candidates": 0, "alive": 0, "dead": 0})
            m.update(v)
        jpath, mpath = report_mod.write_reports(
            os.path.join(HERE, "reports"),
            {"summary": summary, "per_source": merged, "per_protocol": {}, "fastest": fastest},
            stamp,
        )

        print("\n" + "=" * 66)
        print("  SELESAI" + ("  (DRY-RUN, DB tak diubah)" if args.dry_run else ""))
        print("=" * 66)
        for k in ("duration_s", "rechecked", "alive_old", "new_alive", "inserted",
                  "pruned", "held_grace", "http_total", "http_alive", "vercel",
                  "cloudflare", "integrity", "guard_ok"):
            print(f"   {k:14s} {summary[k]}")
        print(f"\n   laporan : {mpath}")
        print(f"   data    : {jpath}")
        return 0
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(
        description="proxy-farm-9router: farm proxy publik, recheck, bersihkan, inject ke 9router"
    )
    p.add_argument("--dry-run", action="store_true",
                   help="uji semuanya, jangan tulis ke DB")
    p.add_argument("--no-prune", action="store_true", help="jangan hapus baris mati")
    p.add_argument("--no-order", action="store_false", dest="do_order", help="jangan susun ulang")
    p.add_argument("--mode", choices=["deep", "tcp"], default=None,
                   help="deep = TCP + exit-IP (default), tcp = TCP saja")
    p.add_argument("--limit-sources", default=None, help="id sumber, koma dipisah")
    args = p.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\ndibatalkan", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
