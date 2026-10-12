# proxy-farm-9router

Bot satu klik: kumpulkan proxy publik dari 10 sumber, **recheck** yang sudah
ada di DB 9router, **buang yang mati**, naikkan yang hidup, dan rapikan
tampilannya.

> **Peringatan.** Proxy publik berarti operatornya bisa membaca lalu lintas yang
> melewatinya. Jangan mengarahkannya ke request yang membawa API key provider,
> token, atau isi prompt skripsi.

## Cara Pakai (Benar-benar 1 Klik)

Tinggal **klik ganda** ikon **Proxy Farm 9Router** di Desktop:
- Langsung jalan otomatis tanpa perlu pilih-pilih menu.
- Recheck DB ➔ bersihkan proxy mati ➔ ambil proxy fresh dari 10 repo ➔ uji HTTPS ➔ masukkan ke 9router.
- Menampilkan progres & ringkasan hijau saat selesai, lalu jendela menutup otomatis.

Untuk penggunaan manual / debug via terminal:

```bash
./run.sh                    # sama seperti klik ganda (langsung sync)
python3 farm.py --dry-run   # uji saja tanpa menyentuh database
python3 verify_9router.py   # uji sampel proxy aktif ke endpoint AI OpenRouter
python3 reorder.py          # rapikan urutan tampilan di 9router
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
- **Uji nyata** — lewati proxy, ambil IP publik dari endpoint netral.

Tiga hal yang ditemukan lewat debugging dan naik pangkat jadi aturan:

1. **Endpoint uji harus HTTP, bukan HTTPS.** Proxy HTTP biasa tidak bisa
   melakukan CONNECT TLS, jadi HTTPS selalu gagal `WRONG_VERSION_NUMBER`
   walau proxynya sehat.
2. **`asyncio.wait_for` wajib membungkus request httpx.** Timeout milik httpx
   tidak membatasi handshake SOCKS5 — satu proxy yang menggantung bisa
   menahan sesi berjam-jam. Tanpa `wait_for`, 5.187 baris tidak pernah tuntas;
   dengannya, 1,4 menit.
3. **"Bisa balas IP" BUKAN bukti proxy sehat — ini yang paling penting.**
   Uji kelayakan harus menuntut **lebih dari satu host berbeda** yang berhasil.
   Uji lapangan: 38 dari 40 proxy "teratas" yang tercatat hidup ternyata palsu —
   cuma bisa menjangkau satu tujuan, lalu mengembalikan halaman blokir
   Cloudflare (`error code: 1003`, header `CF-RAY`) untuk host lain. Proxy
   begitu tampak hidup sepenuhnya kalau cuma diuji satu endpoint. Itu sebabya
   `src/testers.py` menolak proxy yang `hosts_ok < 2` dengan alasan
   `hanya 1 host ok — proxy palsu?`.

Konsekuensinya: jumlah "aktif" yang dilaporkan ke DB jauh lebih sedikit daripada
sebelumnya. Pada sampel 300 proxy bertanda aktif, hanya **116 (38,7%)** yang
benar-benar bisa meneruskan ke dua host berbeda — dengan 112 exit IP unik.
Itu angka jujur, bukan penurunan kualitas alat.

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
