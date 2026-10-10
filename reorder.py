#!/usr/bin/env python3
"""Susun ulang tampilan proxy di 9router tanpa farm apa pun.

Dipakai dari menu run.sh (pilihan 3) untuk memastikan blok urutannya tetap
custom -> vercel -> cloudflare tanpa perlu menjalankan proses panjang.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tomllib

from src import db as dbmod


def main() -> int:
    cfg = tomllib.load(open(os.path.join(HERE, "config.toml"), "rb"))
    con = dbmod.connect(cfg["database"]["path"])
    con.execute("PRAGMA busy_timeout=60000")
    try:
        before = {r["id"]: r["updatedAt"] for r in dbmod.row_view(con)
                  if r["type"] in ("vercel", "cloudflare")}
        con.execute("BEGIN IMMEDIATE")
        n = dbmod.order_rows(con, cfg["order"]["groups"])
        con.execute("COMMIT")

        after = {r["id"]: r["updatedAt"] for r in dbmod.row_view(con)
                 if r["type"] in ("vercel", "cloudflare")}
        touched = [i for i, ts in after.items() if before.get(i) != ts]

        rows = dbmod.row_view(con)
        kinds: dict[str, int] = {}
        for r in rows:
            kinds[r["type"]] = kinds.get(r["type"], 0) + 1
        print(f"  {n} baris disusun: " + " -> ".join(cfg["order"]["groups"]))
        print("  isi DB: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
        print(f"  rangkap vercel/cloudflare: "
              f"{'✅ tak tersentuh' if not touched else f'❌ {len(touched)} baris berubah'}")
        return 0 if not touched else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
