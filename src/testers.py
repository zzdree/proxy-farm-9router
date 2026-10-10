"""Engine pengujian: tahap 1 TCP connect, tahap 2 exit-IP & anonimitas.

Mode memakai `asyncio.open_connection`, jadi uji TCP tidak mengirim
data aplikasi apa pun ke proxy maupun ke endpoint uji.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx


@dataclass
class TestResult:
    alive: bool
    latency_ms: int | None = None
    exit_ip: str | None = None
    leak: bool | None = None
    reason: str = ""

    def as_dict(self):
        return {
            "alive": self.alive,
            "latency_ms": self.latency_ms,
            "exit_ip": self.exit_ip,
            "leak": self.leak,
            "reason": self.reason,
        }


async def _tcp_probe(host: str, port: int, timeout: float) -> TestResult:
    t0 = time.monotonic()
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return TestResult(False, reason="timeout")
    except ConnectionRefusedError:
        return TestResult(False, reason="refused")
    except OSError as exc:
        return TestResult(False, reason=f"os:{exc.errno or 'err'}")
    finally:
        pass
    try:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except Exception:
        pass
    return TestResult(True, latency_ms=int((time.monotonic() - t0) * 1000))


async def _exit_ip(proxy_url: str, timeout: float, local_ip: str | None) -> tuple:
    """Uji apakah proxy benar-benar meneruskan request dan tidak membocorkan IP lokal."""
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url, timeout=timeout, verify=False, trust_env=False
        ) as client:
            r = await client.get("https://api.ipify.org")
            if r.status_code != 200:
                return None, True, f"status:{r.status_code}"
            exit_ip = r.text.strip()
    except Exception as exc:
        return None, False, f"{type(exc).__name__}"
    leak = bool(local_ip and exit_ip == local_ip)
    return exit_ip, leak, ""


async def test_one(cand, mode: str, timeout: float, local_ip: str | None) -> tuple:
    """Balikkan (candidate, TestResult). Tak pernah melempar exception."""
    res = await _tcp_probe(cand.host, cand.port, timeout)
    if not res.alive:
        return cand, res

    if mode == "exitip":
        exit_ip, leak, why = await _exit_ip(cand.proxy_url, timeout, local_ip)
        res.exit_ip = exit_ip
        res.leak = leak
        if leak:
            # Proxy membocorkan IP asli: tidak layak dipakai.
            res.alive = False
            res.reason = "leak:exit_ip_sama_dengan_ip_lokal"
        elif exit_ip is None:
            res.alive = False
            res.reason = f"no_exit_ip:{why}"
    return cand, res


async def test_all(cands, mode: str, timeout: float, concurrency: int, local_ip: str | None):
    print(f"  [3] TEST {len(cands)} kandidat (mode={mode}, concur={concurrency}) ...")
    sem = asyncio.Semaphore(concurrency)

    async def guarded(c):
        async with sem:
            return await test_one(c, mode, timeout, local_ip)

    results = []
    done = 0
    total = len(cands)
    for fut in asyncio.as_completed([guarded(c) for c in cands]):
        pair = await fut
        results.append(pair)
        done += 1
        if done % 500 == 0 or done == total:
            print(f"      {done}/{total} selesai")
    alive = sum(1 for _, r in results if r.alive)
    print(f"  [3b] hasil: {alive} hidup / {total - alive} mati")
    return results
