# proxy-farm-9router

Bot satu klik: kumpulkan proxy publik dari 10 sumber, **recheck** yang sudah
ada di DB 9router, **buang yang mati**, naikkan yang hidup, dan rapikan
tampilannya.

> **Peringatan.** Proxy publik berarti operatornya bisa membaca lalu lintas yang
> melewatinya. Jangan mengarahkannya ke request yang membawa API key provider,
> token, atau isi prompt skripsi.

## Pakai (1 klik)

Ada ikon **Proxy Farm** di Desktop. Klik ganda → pilih menu:

| pilih | apa yang terjadi |
|---|---|
| **1** | farm penuh — recheck semua baris lama, uji kandidat baru, buang yang mati, inject yang hidup |
| **2** | dry-run — jalankan semua uji, DB **tidak disentuh**. Buat lihat hasil sebelum berkomitmen |
| **3** | cuma reorder — susun tampilan di 9router tanpa farm apa pun |

Atau dari terminal:

```bash
./run.sh                    # menu interaktif
python3 farm.py --dry-run   # langsung dry-run
python3 farm.py             # langsung farm penuh
python3 reorder.py          # langsung reorder
```

## Apa yang bot ini perlakukan sebagai suci

**vercel dan cloudflare tidak pernah disentuh.** Keduanya adalah relay yang kamu
deploy sendiri, bukan hasil farm. Perlindungannya tiga lapis:

1. `config.toml [scope]` — hanya `type=http` yang boleh recheck/prune
2. `src/db.py` — `order_rows()` hanya menyentuh type yang disebut di `[order].groups`
3. `farm.py` — bandingkan `updatedAt` vercel/cloudflare sebelum dan sesudah;
   **kalau satu saja berubah, run gagal keras** (`RANGKAP MELANGAR`)

Baru saja: selama pengembangan, skrip sekali menggeser blok custom ke bawah.
Bug itu ketangkap lapisan ke-3 — karena itu lapisan itu dibuat lebih dulu.

## Inti alur

```
backup DB  →  fetch 10 sumber  →  dedup
     →  recheck baris http lama  →  mati? strike++
     →  grace 2 strike baru dibuang  →  uji kandidat baru  →  yang lolos masuk
     →  reorder (custom → vercel → cloudflare)  →  laporan
```

Prune cuma milik tool (baris bertanda `importedSource`), jadi 335 custom
pertamamu tidak ikut terbawa. Kalau suatu saat kamu bind koneksi ke suatu
proxy, bot akan **lewatkan** baris itu — 9router akan menolak hapus baris
yang sedang dipakai (HTTP 409), dan bot lebih baik jujur daripada memaksa.

## Sumber (10, terverifikasi hidup)

`monosans` · `geonode` · `proxyscrape` (http+socks5) · `proxifly` (http+socks5)
· `TheSpeedX` (http+socks4+socks5) · `clarketm`

Tambah/keluarkan sumber = edit `sources.json`, tanpa sentuh kode.

## Soal pengujian

Dua tahap:

- **TCP connect** — murah, tak mengirim data ke mana pun. Memilah banyak kandidat.
- **Uji nyata** (exit-IP) — lewati proxy, ambil IP publik dari endpoint netral.

Dua hal yang ditemukan lewat debugging dan kenaikan menjadi aturan:

1. **Endpoint uji harus HTTP, bukan HTTPS.** Proxy HTTP biasa tidak bisa
   melakukan CONNECT TLS, jadi HTTPS selalu gagal `WRONG_VERSION_NUMBER`
   walau proxynya sehat.
2. **`asyncio.wait_for` wajib membungkus request httpx.** Timeout milik httpx
   tidak membatasi handshake SOCKS5 — satu proxy yang menggantung bisa
   menahan sesi berjam-jam. Tanpa `wait_for`, 5.187 baris tidak pernah tuntas;
   dengannya, 1,4 menit.

Proxy yang exit-IP-nya sama dengan IP mesin kamu ditolak: proxy itu tak
memberi harga apa pun dan hanya menambah hop.

**socks4 tidak bisa diuji** di sini — httpx tidak mengenali scheme-nya, jadi
baris socks4 hanya lolos berdasarkan TCP dan akan gagal saat dipakai.

Proxy SOCKS5 diuji sungguhan. `socksio` dibutuhkan dan sudah diletakkan di
`.pylibs/` (bukan di Python sistem, karena PEP 668 memblokir pip di distro ini).

## Tipe proxy di 9router

9router hanya menerima lima nilai `type`: `http`, `vercel`, `cloudflare`,
`deno`, `netlify`; nilai lain dilempar server jadi `http`. Karena itu bot
menulis semuanya sebagai `type: "http"` dan protokol aslinya disimpan di
`importedProto` (nama barisnya jadi `farm-socks5-…`, `farm-http-…`, dst.).
URL `socks5://` sendiri utuh dan memang didukung undici.

## File

```
run.sh             menu interaktif (dipakai shortcut Desktop)
farm.py            orchestrator: farm → recheck → prune → inject → reorder
reorder.py         susun ulang tampilan saja
sources.json       daftar 10 sumber
config.toml        batas konkurensi, mode uji, path DB, retensi backup, scope
src/fetch.py       fetch + normalisasi + dedup
src/testers.py     engine uji (TCP + exit-IP)
src/db.py          penulis DB (transaksi, rangkap, urutan)
src/report.py      laporan JSON + Markdown
src/util.py        validasi endpoint (host:port)
.pylibs/           socksio (lokal, tak menyentuh sistem)
```

Cadangan DB diambil sebelum setiap run ke `backups/pre-run-*` (10 terakhir).
Integritas DB diperiksa di akhir tiap run dan dicatat di laporan.
