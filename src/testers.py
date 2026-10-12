"""Engine pengujian proxy.

Tahap 1: TCP probe (cepat, memilah host mati dalam milidetik tanpa kirim data).
Tahap 2: Live HTTPS probe ke https://api.ipify.org (fallback https://icanhazip.com).
         - Menguji kemampuan tunneling HTTPS CONNECT (kebutuhan mutlak 9router).
         - Validasi respons adalah IP valid menggunakan ipaddress (menolak HTML / error Cloudflare).
         - Anti-leak check (exit_ip != local_ip).
         - Timeout dibatasi ketat dengan asyncio.wait_for untuk mencegah socket gantung.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass

import httpx

_HTTPX_SCHEMES = {"http", "https", "socks5"}

_ENDPOINTS = [
    "https://api.ipify.org",
    "https://icanhazip.com",
]


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


async def _get_via_proxy(proxy_url: str, url: str, timeout: float):
    # wait_for pembungkus eksternal itu WAJIB: timeout httpx tidak
    # membatasi handshake SOCKS5, sehingga proxy yang menggantung bisa
    # menahan sesi berjam-jam.
    async def _fetch():
        async with httpx.AsyncClient(
            proxy=proxy_url, timeout=timeout, trust_env=False, verify=False
        ) as client:
            return await client.get(url)

    return await asyncio.wait_for(_fetch(), timeout=timeout + 0.5)


async def tcp_probe(host: str, port: int, timeout: float) -> Verdict:
    """Tahap murah: bisa TCP connect. Tidak mengirim data ke mana pun."""
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


async def live_probe(
    proxy_url: str,
    endpoints: list[str] | None = None,
    timeout: float = 5.0,
    local_ip: str | None = None,
) -> Verdict:
    """Uji nyata HTTPS proxy.

    9router membutuhkan HTTPS tunnel untuk berkomunikasi dengan upstream AI API.
    Proxy harus berhasil melakukan CONNECT TLS dan mengembalikan exit IP yang sah.
    """
    scheme = proxy_url.split("://", 1)[0]
    if scheme not in _HTTPX_SCHEMES:
        return Verdict(False, reason=f"scheme_{scheme}_tak_bisa_diuji_httpx")

    eps = endpoints or _ENDPOINTS
    last_err = ""

    for ep in eps:
        t0 = time.monotonic()
        try:
            r = await _get_via_proxy(proxy_url, ep, timeout)
        except asyncio.TimeoutError:
            last_err = "timeout"
            continue
        except Exception as exc:
            last_err = f"{type(exc).__name__}"
            continue

        if r.status_code != 200:
            last_err = f"status:{r.status_code}"
            continue

        body = (r.text or "").strip()
        try:
            ip_obj = ipaddress.ip_address(body)
            exit_ip = str(ip_obj)
        except ValueError:
            last_err = f"non_ip_body:{body[:30]}"
            continue

        leak = bool(local_ip and exit_ip == local_ip)
        if leak:
            return Verdict(
                False,
                exit_ip=exit_ip,
                leak=True,
                reason="exit_ip == ip lokal (proxy bocor)",
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        return Verdict(
            True,
            exit_ip=exit_ip,
            latency_ms=latency_ms,
            leak=False,
            reason="",
        )

    return Verdict(False, reason=last_err or "no_endpoint_ok")


async def deep_probe(
    proxy_url: str, tcp_timeout: float, deep_timeout: float, local_ip: str | None
) -> Verdict:
    """TCP dulu (murah); kalau lolos, naik ke uji jalur HTTPS sungguhan."""
    host, _, port = proxy_url.rpartition("://")[2].rpartition(":")
    try:
        tcp = await tcp_probe(host, int(port), tcp_timeout)
    except ValueError:
        return Verdict(False, reason="url_tak_terbaca")
    if not tcp.alive:
        return tcp
    return await live_probe(proxy_url, None, deep_timeout, local_ip)
