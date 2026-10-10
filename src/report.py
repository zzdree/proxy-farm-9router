"""Laporan JSON + Markdown."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def _human(n) -> str:
    return f"{n:,}".replace(",", ".")


def stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def write_reports(outdir: str, payload: dict, stamp: str) -> tuple[str, str]:
    os.makedirs(outdir, exist_ok=True)
    jpath = os.path.join(outdir, f"farm-{stamp}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False, default=str)

    mpath = os.path.join(outdir, f"farm-{stamp}.md")
    s = payload["summary"]

    lines = [
        f"# proxy-farm — {stamp}",
        "",
        "## Ringkasan",
        "",
        f"- Durasi                 : **{s.get('duration_s')} s**",
        f"- Kandidat terkumpul     : **{_human(s.get('candidates', 0))}** "
        f"(baru: {_human(s.get('fresh', 0))})",
        f"- Recheck baris lama     : **{_human(s.get('rechecked', 0))}** "
        f"→ hidup **{_human(s.get('alive_old', 0))}**, mati **{_human(s.get('dead_old', 0))}**",
        f"- Kandidat baru hidup     : **{_human(s.get('new_alive', 0))}**",
        f"- Masuk ke DB            : **{_human(s.get('inserted', 0))}**",
        f"- Dipangkas (mati)       : **{_human(s.get('pruned', 0))}** "
        f"(ditahan tenggat: {_human(s.get('held_grace', 0))})",
        f"- DB setelah run         : http **{_human(s.get('http_total', 0))}** "
        f"(hidup {_human(s.get('http_alive', 0))}) · vercel **{_human(s.get('vercel', 0))}** "
        f"· cloudflare **{_human(s.get('cloudflare', 0))}**",
        f"- Integritas DB          : **{s.get('integrity')}** · rangkap vercel/cloudflare: "
        f"{'✅ tak tersentuh' if s.get('guard_ok') else '❌ PELANGGARAN'}",
        "",
        "## Per sumber (kandidat baru)",
        "",
        "| sumber | kandidat | hidup | mati |",
        "|---|---:|---:|---:|",
    ]
    for sid, v in sorted(payload.get("per_source", {}).items()):
        lines.append(
            f"| {sid} | {_human(v.get('candidates', 0))} | "
            f"{_human(v.get('alive', 0))} | {_human(v.get('dead', 0))} |"
        )

    fastest = payload.get("fastest") or []
    if fastest:
        lines += ["", "## 15 tercepat", "", "| proxy | protokol | latensi | sumber |",
                  "|---|---|---:|---|"]
        for r in fastest[:15]:
            lines.append(
                f"| `{r['proxyUrl']}` | {r.get('protocol')} | "
                f"{r.get('latencyMs')} ms | {r.get('source')} |"
            )
    lines.append("")
    with open(mpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return jpath, mpath
