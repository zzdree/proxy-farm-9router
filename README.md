# zzproxy-farm

Auto-farm proxy publik → uji → inject ke DB 9router, dengan laporan tiap sesi.

> **Peringatan keamanan.** Proxy publik berarti operatornya bisa melihat lalu lintas
> yang melewatinya. Jangan pernah mengarahkan mereka ke request yang membawa API key
> provider, token, atau isi prompt dari skripsi. Lihat [Keamanan](#keamanan).

## Cara pakai

```bash
pip install -r requirements.txt

# Lihat dulu tanpa menulis apa pun (juara fitur)
python3 farm.py --dry-run

# Farm + uji TCP + inject ke DB 9router
python3 farm.py

# Uji lebih dalam (sudah jadi parameter, lihat catatan mode)
python3 farm.py --mode exitip

# Batasi sumber tertentu saja
python3 farm.py --limit-sources monosans,proxyscrape-http
```

Setiap yang jalan menulis laporan ke `reports/` dan daftar hidup ke `data/`.

## Sumber (10, sudah diverifikasi hidup)

| id | sumber | parser |
|---|---|---|
| `monosans` | github.com/monosans/proxy-list | JSON + metadata |
| `geonode` | proxylist.geonode.com | JSON + metadata |
| `proxyscrape-{http,socks5}` | api.proxyscrape.com | text |
| `proxifly-{http,socks5}` | github.com/proxifly/free-proxy-list | text |
| `thespeedx-{http,socks4,socks5}` | github.com/TheSpeedX/PROXY-List | text |
| `clarketm` | github.com/clarketm/proxy-list | text |

Menambah sumber = tambah entri di `sources.json`, tanpa ubah kode.

## Pipeline

```
fetch (10 sumber, paralel)  →  dedup  →  uji  →  filter  →  inject  →  laporan
```

Hasil run terakhir (8.000 kandidat): **3.409 hidup (42,6%)** dalam ±77 detik.
Sumber paling melimpah justru paling buruk — `proxifly-socks5` menyumbang kontribusi
hidup terendah per kandidat; `monosans` yang kecil paling rajin (38% hidup).

## Mode uji

| mode | apa yang dilakukan | kapan dipakai |
|---|---|---|
| `tcp` | cek koneksi TCP saja. Tidak mengirim data apa pun ke luar. | default: memilah ribuan kandidat cepat |
| `exitip` | cek TCP + exit IP + **tolak yang membocorkan IP lokal** | untuk menguji kebocoran |

Mode `exitip` sengaja menolak proxy yang exit IP-nya sama dengan IP mesin kamu:
proxy seperti itu tidak memberi harga apa pun dan hanya menambah hop.

## Soal proxy SOCKS

9router hanya menerima lima nilai `type` — `http`, `vercel`, `cloudflare`, `deno`,
`netlify`; nilai lain dilempar paksa menjadi `http` oleh server. Karena itu farm
menulis semuanya sebagai `type: "http"` dan menyimpan protokol asalnya di
`importedProto` (nama file menjadi `farm-socks5-…`, `farm-http-…`, dsb). URL
`socks5://` sendiri tetap utuh dan memang didukung undici, jadi proxy socks5
tetap bisa dipakai — yang dibatasi hanya labelnya.

## Urutan tampilan

9router mengurutkan daftar proxy `ORDER BY updatedAt DESC`. `farm.py` menyusun:

```
custom (type=http)  →  vercel  →  cloudflare
```

Tiap baris dapat stempel unik yang mundur 1 ms, jadi urutannya deterministik
(tanpa tie), dan baseline-nya diambil dari `updatedAt` teratas yang sudah ada —
bukan jam dinding, supaya tidak rusak akibat perbedaan zona waktu.

## Keamanan

- Cadangan otomatis `backups/pre-farm-*/` sebelum sesi inject, dirotasi (10 terakhir).
- Tulis DB lewat `BEGIN IMMEDIATE` + `busy_timeout=60s`; 9router **tidak perlu dihentikan**.
- Mode `tcp` tidak mengirim data ke mana pun; mode `exitip` hanya menyentuh
  endpoint netral `api.ipify.org`.
- `integrity_check` dijalankan setelah setiap inject dan dicatat di laporan.

## File

```
farm.py           orchestrator CLI
sources.json      daftar sumber (10)
config.toml       batas konkurensi, mode uji, path DB, retensi backup
src/fetch.py      fetch + normalisasi + dedup
src/testers.py    engine uji (tcp / exitip)
src/injected.py   penulis DB (transaksi, urutan)
src/report.py     laporan JSON + Markdown
src/util.py       validasi endpoint (host:port)
```
