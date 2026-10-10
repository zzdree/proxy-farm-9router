"""Laporan JSON + Markdown supaya hasil tiap sesi bisa diaudit."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .util import human


def write_reports(outdir: str, payload: dict, stamp: str) -> tuple[str, str]:
    os.makedirs(outdir, exist_ok=True)
    jpath = os.path.join(outdir, f"farm-{stamp}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)

    mpath = os.path.join(outdir, f"farm-{stamp}.md")
    s = payload["summary"]
    lines = [
        f"# zzproxy-farm — {stamp}",
        "",
        "## Ringkasan",
        "",
        f"- Kandidat dari {s['sources_total']} sumber  : **{human(s['candidates'])}**",
        f"- Unik setelah dedup           : **{human(s['unique'])}**",
        f"- Hidup setelah uji            : **{human(s['alive'])}**"
        + (f" ({s['alive_pct']}%)" if s.get("alive_pct") is not None else ""),
        f"- Mati / timeout / bocor       : **{human(s['dead'])}**",
        f"- Baru dimasukkan ke 9router   : **{human(s['inserted'])}**",
        f"- Dilewati (sudah ada di DB)  : **{human(s['skipped'])}**",
        f"- Total proxy custom di DB    : **{human(s['db_custom_total'])}**",
        "",
        "## Per sumber",
        "",
        "| sumber | kandidat | hidup | gagal |",
        "|---|---:|---:|---:|",
    ]
    for sid, v in sorted(payload["per_source"].items()):
        lines.append(
            f"| {sid} | {human(v['candidates'])} | {human(v['alive'])} | {human(v['dead'])} |"
        )
    lines += ["", "## Protokol", "", "| protokol | hidup |", "|---|---:|"]
    for k, v in sorted(payload["per_protocol"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {human(v)} |")
    lines += ["", "## 15 tercepat", "", "| proxy | protokol | latensi | sumber |", "|---|---|---:|---|"]
    for r in payload["fastest"][:15]:
        lines.append(
            f"| `{r['proxyUrl']}` | {r['protocol']} | {r['latencyMs']} ms | {r['source']} |"
        )
    lines.append("")
    with open(mpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return jpath, mpath


def stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
