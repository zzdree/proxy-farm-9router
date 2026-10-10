"""Pengambil & penormat kandidat proxy dari semua sumber."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, asdict
from typing import Iterable

import httpx

from .util import parse_endpoint


@dataclass(frozen=True)
class Candidate:
    proxy_url: str
    protocol: str
    source: str
    host: str
    port: int

    def as_dict(self):
        return asdict(self)


def _dedupe(cands: Iterable[Candidate]) -> list[Candidate]:
    """Deduplikasi berdasarkan proxy_url. Entri pertama menang (sumber prioritas)."""
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in cands:
        if c.proxy_url in seen:
            continue
        seen.add(c.proxy_url)
        out.append(c)
    return out


def _parse_line_ip_port(text: str, source: str, default_proto) -> Iterable[Candidate]:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Format "ip:port", atau "proto://ip:port" bila sumber menyertakannya.
        proto = default_proto
        if "://" in line:
            scheme, _, rest = line.partition("://")
            scheme = scheme.lower()
            if scheme in ("http", "https", "socks4", "socks5"):
                proto = "https" if scheme == "https" else scheme
            line = rest
        # Buang kredensial "user:pass@host:port" bila ada.
        if "@" in line:
            line = line.rsplit("@", 1)[1]
        ep = parse_endpoint(line)
        if ep is None:
            continue
        host, port = ep
        yield Candidate(
            proxy_url=f"{proto}://{host}:{port}",
            protocol=proto,
            source=source,
            host=host,
            port=port,
        )


def _parse_monosans(payload: bytes, source: str) -> Iterable[Candidate]:
    for row in json.loads(payload):
        host, port = row.get("host"), row.get("port")
        proto = (row.get("protocol") or "http").lower()
        if proto == "https":
            proto = "https"
        ep = parse_endpoint(f"{host}:{port}") if host and port else None
        if ep is None:
            continue
        yield Candidate(
            proxy_url=f"{proto}://{ep[0]}:{ep[1]}",
            protocol=proto,
            source=source,
            host=ep[0],
            port=ep[1],
        )


def _parse_geonode(payload: bytes, source: str) -> Iterable[Candidate]:
    data = json.loads(payload).get("data") or []
    for row in data:
        host, port = row.get("ip"), row.get("port")
        protos = row.get("protocols") or ["http"]
        ep = parse_endpoint(f"{host}:{port}") if host and port else None
        if ep is None:
            continue
        proto = (protos[0] or "http").lower()
        yield Candidate(
            proxy_url=f"{proto}://{ep[0]}:{ep[1]}",
            protocol=proto,
            source=source,
            host=ep[0],
            port=ep[1],
        )


_PARSERS = {
    "text_ipport": _parse_line_ip_port,
    "monosans_json": _parse_monosans,
    "geonode_json": _parse_geonode,
}


async def _fetch_one(client: httpx.AsyncClient, spec: dict) -> list[Candidate]:
    sid, url = spec["id"], spec["url"]
    parser_name = spec.get("parser", "text_ipport")
    proto = spec.get("protocol")
    parser = _PARSERS.get(parser_name)
    if parser is None:
        raise ValueError(f"parser tidak dikenal: {parser_name}")
    try:
        resp = await client.get(url, timeout=25.0, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # jaringan/sumber mati: jangan gagalkan seluruh farm
        print(f"    [{sid}] GAGAL: {type(exc).__name__}: {str(exc)[:70]}")
        return []
    if parser_name == "text_ipport":
        cands = list(parser(resp.text, sid, proto))
    else:
        cands = list(parser(resp.content, sid))
    print(f"    [{sid}] {len(cands)} kandidat ({len(resp.content):,} B)")
    return cands


async def gather(sources: list[dict], max_candidates: int, concurrency: int) -> list[Candidate]:
    """Ambil semua sumber paralel, lalu dedup. Aman walau sumber mati."""
    print("  [1] FETCH kandidat:")
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        limits=limits, headers={"User-Agent": "Mozilla/5.0 zzproxy-farm/1.0"}
    ) as client:
        results = await asyncio.gather(
            *[_fetch_one(client, s) for s in sources if s.get("enabled", True)],
            return_exceptions=True,
        )

    all_c: list[Candidate] = []
    for r in results:
        if isinstance(r, Exception):
            print(f"    (sumber dilewati: {type(r).__name__})")
            continue
        all_c.extend(r)

    unique = _dedupe(all_c)
    print(f"  [2] DEDUP: {len(all_c)} -> {len(unique)} unik")
    if max_candidates and len(unique) > max_candidates:
        unique = unique[:max_candidates]
        print(f"  [2b] dibatasi max_candidates -> {len(unique)}")
    return unique
