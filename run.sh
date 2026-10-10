#!/usr/bin/env bash
# proxy-farm — jalankan dengan satu klik. Jendela terminal tetap terbuka
# sampai kamu tekan Enter, supaya hasil run bisa dibaca dulu.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# socksio dipasang di .pylibs supaya tak menyentuh Python sistem (PEP 668).
export PYTHONPATH="$PWD/.pylibs${PYTHONPATH:+:$PYTHONPATH}"

C_GRN=$'\033[1;32m'; C_YEL=$'\033[1;33m'; C_RED=$'\033[1;31m'
C_CYN=$'\033[1;36m'; C_RST=$'\033[0m'

echo "${C_CYN}"
echo "  ╔════════════════════════════════════════════════════╗"
echo "  ║              PROXY FARM  —  9router                ║"
echo "  ╚════════════════════════════════════════════════════╝"
echo "${C_RST}"
echo "  1) Farm penuh   (recheck + farm baru + bersihkan mati)"
echo "  2) Dry-run      (uji semuanya, DB TIDAK diubah)"
echo "  3) Cuma reorder (urutkan tampilan di 9router)"
echo "  4) Keluar"
echo
read -rp "  Pilih [1-4]: " pilihan

case "$pilihan" in
  1) CMD=(python3 farm.py) ;;
  2) CMD=(python3 farm.py --dry-run) ;;
  3) CMD=(python3 reorder.py) ;;
  *) echo "Keluar."; sleep 1; exit 0 ;;
esac

echo
"${CMD[@]}"
status=$?

echo
if [ $status -eq 0 ]; then
  echo "${C_GRN}  ✅ Selesai.${C_RST}"
else
  echo "${C_RED}  ❌ Ada masalah (kode $status). Lihat pesan di atas.${C_RST}"
fi
echo
read -rp "  Tekan Enter untuk menutup..." _
