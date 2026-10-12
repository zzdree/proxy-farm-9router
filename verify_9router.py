#!/usr/bin/env python3
"""verify_9router — Uji kelayakan proxy untuk 9router secara nyata.

Arsitektur 9router:
  Klien -> 9router (localhost:20128) -> PROXY (CONNECT tunnel) -> AI Provider (HTTPS)

Skrip ini menguji apakah proxy mampu:
  1. Melakukan HTTPS CONNECT tunneling ke endpoint publik (https://api.ipify.org).
  2. Menjangkau endpoint API AI provider (https://openrouter.ai/api/v1/models).
  3. Memastikan tidak membocorkan IP lokal dan bukan proxy palsu (Cloudflare block).
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import sys
import time
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LOCAL_LIBS = os.path.join(HERE, ".pylibs")
if os.path.isdir(LOCAL_LIBS) and LOCAL_LIBS not in sys.path:
    sys.path.insert(0, LOCAL_LIBS)

import tomllib
import httpx

from src import db as dbmod
from src import util


@dataclass
class VerifyResult:
    proxy_url: str
    protocol: str
    tcp_ok: bool
    https_ok: bool
    ai_api_ok: bool
    latency_ms: int | None
    exit_ip: str | None
    error: str = ""


async def test_single_proxy(proxy_url: str, local_ip: str | None, timeout: float = 6.0) -> VerifyResult:
    proto = proxy_url.split("://", 1)[0]
    host, _, port_s = proxy_url.rpartition("://")[2].rpartition(":")
    port = int(port_s)

    # 1. TCP Connect
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3.0
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        tcp_ok = True
    except Exception as e:
        return VerifyResult(proxy_url, proto, False, False, False, None, None, f"TCP {type(e).__name__}")

    # 2. HTTPS Tunnel ke ipify (Verifikasi CONNECT + Exit IP)
    t1 = time.monotonic()
    exit_ip = None
    https_ok = False
    try:
        async def _probe_ip():
            async with httpx.AsyncClient(
                proxy=proxy_url, timeout=timeout, trust_env=False, verify=False
            ) as c:
                return await c.get("https://api.ipify.org")

        r = await asyncio.wait_for(_probe_ip(), timeout=timeout + 0.5)
        if r.status_code == 200:
            body = r.text.strip()
            try:
                ip_obj = ipaddress.ip_address(body)
                exit_ip = str(ip_obj)
                if local_ip and exit_ip == local_ip:
                    return VerifyResult(proxy_url, proto, True, False, False, None, exit_ip, "IP_LEAK")
                https_ok = True
            except ValueError:
                return VerifyResult(proxy_url, proto, True, False, False, None, None, f"NON_IP_BODY:{body[:25]}")
        else:
            return VerifyResult(proxy_url, proto, True, False, False, None, None, f"HTTPS_{r.status_code}")
    except asyncio.TimeoutError:
        return VerifyResult(proxy_url, proto, True, False, False, None, None, "HTTPS_Timeout")
    except Exception as e:
        return VerifyResult(proxy_url, proto, True, False, False, None, None, f"{type(e).__name__}")

    latency_ms = int((time.monotonic() - t1) * 1000)

    # 3. AI Provider Reachability (OpenRouter models endpoint)
    ai_api_ok = False
    try:
        async def _probe_ai():
            async with httpx.AsyncClient(
                proxy=proxy_url, timeout=timeout, trust_env=False, verify=False
            ) as c:
                return await c.get("https://openrouter.ai/api/v1/models")

        r_ai = await asyncio.wait_for(_probe_ai(), timeout=timeout + 0.5)
        if r_ai.status_code == 200 and len(r_ai.content) > 1000:
            ai_api_ok = True
    except Exception:
        ai_api_ok = False

    return VerifyResult(proxy_url, proto, tcp_ok, https_ok, ai_api_ok, latency_ms, exit_ip, "")


async def main_async(args) -> int:
    cfg = tomllib.load(open(os.path.join(HERE, "config.toml"), "rb"))
    db_path = cfg["database"]["path"]

    local_ip = "114.12.15.166"

    # Ambil proxy dari DB atau argument
    if args.urls:
        urls = args.urls
    else:
        con = dbmod.connect(db_path)
        try:
            rows = dbmod.row_view(con)
            http_rows = [r for r in rows if r["type"] == "http"]
            if args.active_only:
                http_rows = [r for r in http_rows if r["testStatus"] == "active"]
            http_rows.sort(key=lambda r: (r["data"].get("latencyMs") is None, r["data"].get("latencyMs") or 9e9))
            urls = [r["proxyUrl"] for r in http_rows[:args.n]]
        finally:
            con.close()

    if not urls:
        print("Tidak ada proxy yang ditemukan untuk diuji.")
        return 1

    print("=" * 72)
    print(f"  VERIFIKASI PROXY 9ROUTER — {len(urls)} TARGET")
    print(f"  Uji: TCP Connect -> HTTPS Tunnel (ipify) -> AI Provider (OpenRouter)")
    print("=" * 72)

    sem = asyncio.Semaphore(args.concurrency)

    async def worker(u):
        async with sem:
            return await test_single_proxy(u, local_ip, args.timeout)

    t0 = time.monotonic()
    tasks = [worker(u) for u in urls]
    results: list[VerifyResult] = []
    done = 0
    total = len(tasks)

    for fut in asyncio.as_completed(tasks):
        res = await fut
        results.append(res)
        done += 1
        if done % 10 == 0 or done == total:
            ok_cnt = sum(1 for r in results if r.https_ok)
            print(f"\r  Progress: {done}/{total} | Verified HTTPS: {ok_cnt}", end="", flush=True)
    print()

    total_time = round(time.monotonic() - t0, 1)

    https_passed = [r for r in results if r.https_ok]
    ai_passed = [r for r in results if r.ai_api_ok]

    print("\n" + "─" * 72)
    print(f"  HASIL PENGUJIAN ({total_time}s):")
    print(f"  Total diuji              : {len(results)}")
    print(f"  TCP Konek Berhasil       : {sum(1 for r in results if r.tcp_ok)}")
    print(f"  HTTPS Tunnel Berhasil    : {len(https_passed)} ({round(len(https_passed)*100/len(results), 1)}%)")
    print(f"  AI Provider (OpenRouter) : {len(ai_passed)}")
    print("─" * 72)

    if https_passed:
        https_passed.sort(key=lambda x: x.latency_ms or 9e9)
        print("\n  🏆 PROXY TERBAIK (Siap Dipakai 9router):")
        print(f"  {'PROTO':<8} {'LATENSI':<10} {'EXIT IP':<18} {'AI API':<8} {'PROXY URL'}")
        print("  " + "-" * 68)
        for r in https_passed[:20]:
            ai_badge = "✅ YA" if r.ai_api_ok else "⚠️ - "
            print(f"  {r.protocol:<8} {str(r.latency_ms)+'ms':<10} {str(r.exit_ip):<18} {ai_badge:<8} {r.proxy_url}")

    # Simpan hasil ke reports
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_file = os.path.join(HERE, "reports", f"verified-proxies-{stamp}.json")
    with open(report_file, "w") as f:
        json.dump([vars(r) for r in results], f, indent=1)
    print(f"\n  Laporan tersimpan: {report_file}")

    return 0


def main():
    p = argparse.ArgumentParser(description="Verifikasi kelayakan proxy untuk 9router")
    p.add_argument("-n", type=int, default=50, help="Jumlah proxy yang diuji (default: 50)")
    p.add_argument("--active-only", action="store_true", help="Hanya uji proxy yang saat ini bertanda active")
    p.add_argument("--concurrency", type=int, default=25, help="Jumlah worker paralel (default: 25)")
    p.add_argument("--timeout", type=float, default=5.0, help="Timeout per request (default: 5.0s)")
    p.add_argument("--urls", nargs="*", default=None, help="Uji URL spesifik")
    args = p.parse_args()
    try:
        raise SystemExit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        print("\nDibatalkan.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
