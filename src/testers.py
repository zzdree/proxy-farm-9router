"""Engine pengujian proxy: tahap murah (TCP) dan tahap mahal (exit-IP).

Tahap mahal menggunakan httpx dengan proxy nyata, jadi socks5 benar-benar
diuji dengan SOCKS-handshake — bukan cuma konek TCP. socksio dibutuhkan
untuk itu dan sudah diletakkan di .pylibs (lihat bootstrap di farm.py).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx


@dataclass
class Verdict:
    alive: bool
    latency_ms: int | None = None
    exit_ip: str | None = None
    leak: bool = False
    reason: str = ""

    def as_dict(self):
        return {
            "alive": self.alive,
            "latency_ms": self.latency_ms,
            "exit_ip": self.exit_ip,
            "leak": self.leak,
            "reason": self.reason,
        }


async def tcp_probe(host: str, port: int, timeout: float) -> Verdict:
    """Cek paling murah: bisa TCP connect atau tidak. Tidak kirim data apa pun.

    Dipungkus wait_for karena timeout open_connection saja belum cukup:
    socket bisa terkunci di tahap resolve/read pada jaringan bermasalah.
    """
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return Verdict(False, reason="timeout")
    except ConnectionRefusedError:
        return Verdict(False, reason="refused")
    except OSError as exc:
        return Verdict(False, reason=f"os:{exc.errno or 'err'}")
    try:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except Exception:
        pass
    return Verdict(True, latency_ms=int((time.monotonic() - t0) * 1000))


_HTTPX_SCHEMES = {"http", "https", "socks5"}


async def live_probe(
    proxy_url: str, endpoints: list[str], timeout: float, local_ip: str | None
) -> Verdict:
    """Uji nyata: lewati proxy, ambil IP publik dari endpoint netral.

    Endpoint memakai HTTP (bukan HTTPS) karena proxy HTTP biasa tidak bisa
    melakukan CONNECT TLS — HTTPS akan gagal WRONG_VERSION_NUMBER walau
    proxynya sehat. Hanya endpoint yang mengembalikan IP yang dikontak: tak
    ada kredensial, header kustom, atau konten apa pun yang dikirim lewat
    proxy.

    socks4 tidak bisa diuji di sini: httpx tidak mengenali scheme-nya.
    Pemanggil harus menanganinya lebih dulu.
    """
    scheme = proxy_url.split("://", 1)[0]
    if scheme not in _HTTPX_SCHEMES:
        return Verdict(False, reason=f"scheme_{scheme}_tak_bisa_diuji_httpx")
    last_err = ""
    for ep in endpoints:
        try:
            # wait_for pembungkus eksternal itu WAJIB: timeout milik httpx
            # sendiri tidak membatasi handshake SOCKS5, sehingga proxy yang
            # menggantung bisa menahan sesi berjam-jam. Tanpa batas ini,
            # recheck ribuan baris tidak pernah tuntas.
            async def _fetch():
                async with httpx.AsyncClient(
                    proxy=proxy_url, timeout=timeout, trust_env=False, verify=False
                ) as client:
                    return await client.get(ep)

            r = await asyncio.wait_for(_fetch(), timeout=timeout + 0.5)
            if r.status_code != 200:
                last_err = f"status:{r.status_code}"
                continue
            exit_ip = r.text.strip()
            if not exit_ip:
                last_err = "empty_body"
                continue
            leak = bool(local_ip and exit_ip == local_ip)
            return Verdict(
                alive=not leak,
                exit_ip=exit_ip,
                leak=leak,
                reason="exit_ip == ip lokal (proxy tak menutupi)" if leak else "",
            )
        except asyncio.TimeoutError:
            return Verdict(False, reason="timeout")
        except Exception as exc:
            last_err = f"{type(exc).__name__}:{str(exc)[:40]}"
    return Verdict(False, reason=last_err or "no_endpoint_ok")


async def deep_probe(
    proxy_url: str, endpoints: list[str], tcp_timeout: float, deep_timeout: float,
    local_ip: str | None,
) -> Verdict:
    """TCP lebih dulu (murah). Kalau lolos dan proxy termasuk kandidat tercepat,
    naik ke uji exit-IP."""
    host, _, port = proxy_url.rpartition("://")[2].rpartition(":")
    tcp = await tcp_probe(host, int(port), tcp_timeout)
    if not tcp.alive:
        return tcp
    return await live_probe(proxy_url, endpoints, deep_timeout, local_ip)
