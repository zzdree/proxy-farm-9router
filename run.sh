#!/usr/bin/env bash
# proxy-farm-9router — 1-Click Auto Bot
# Klik ganda -> langsung bekerja otomatis tanpa tanya-tanya menu.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# socksio dipasang di .pylibs supaya tak menyentuh Python sistem (PEP 668).
export PYTHONPATH="$PWD/.pylibs${PYTHONPATH:+:$PYTHONPATH}"

C_GRN=$'\033[1;32m'; C_YEL=$'\033[1;33m'; C_RED=$'\033[1;31m'
C_CYN=$'\033[1;36m'; C_RST=$'\033[0m'; C_BLD=$'\033[1m'

clear 2>/dev/null || true
echo "${C_CYN}"
echo "  ╔════════════════════════════════════════════════════╗"
echo "  ║        PROXY FARM 9ROUTER — AUTO SYNC BOT          ║"
echo "  ╚════════════════════════════════════════════════════╝"
echo "${C_RST}"
echo "  🚀 Menjalankan auto-sync..."
echo "     • Recheck & pangkas proxy mati dari DB"
echo "     • Tarik proxy fresh dari 10 sumber GitHub"
echo "     • Uji tunnel HTTPS CONNECT nyata"
echo "     • Masukkan proxy aktif & rapikan urutan"
echo "  ────────────────────────────────────────────────────"
echo

python3 farm.py "$@"
status=$?

echo
echo "  ────────────────────────────────────────────────────"
if [ $status -eq 0 ]; then
  echo "${C_GRN}  ✅ SEMUA BERES! Proxy di 9router sudah segar & aktif.${C_RST}"
else
  echo "${C_RED}  ❌ Selesai dengan catatan (kode $status). Lihat log di atas.${C_RST}"
fi
echo
# Menutup otomatis dalam 15 detik jika tidak disentuh, atau tekan Enter kapan saja
read -t 15 -rp "  [Jendela menutup otomatis dalam 15 detik / Tekan Enter] " _ || echo
