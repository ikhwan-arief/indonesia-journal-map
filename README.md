# Indonesian Journal Atlas

Aplikasi statis (antarmuka berbahasa Inggris) untuk memetakan jurnal Indonesia dalam OpenAlex berdasarkan kota organisasi penerbit. Populasinya adalah seluruh `Source` dengan `type=journal` dan `country_code=ID`. Luas bubble sebanding dengan jumlah jurnal, bukan jumlah artikel.

Browser hanya membaca snapshot lokal. API key OpenAlex tidak pernah dikirim ke frontend.

## Persyaratan

- Python 3.12 atau lebih baru.
- Node.js 22 atau lebih baru untuk pengujian JavaScript.
- API key OpenAlex yang aktif.
- Alamat kontak untuk permintaan Nominatim. Alamat ini dikirim ke layanan geocoding dan tidak dimasukkan ke situs publik.

Tidak ada package Python atau npm yang perlu diinstal.

`GEOCODER_CONTACT` juga dipakai sebagai kontak (`mailto`) untuk Crossref dan DOAJ.

## Menjalankan pipeline

Jangan menulis key pada command line atau file konfigurasi. Pada zsh:

```bash
read -s 'OPENALEX_API_KEY?OpenAlex key: '
export OPENALEX_API_KEY
export GEOCODER_CONTACT='alamat-kontak-anda@example.org'

python3 scripts/collect.py
python3 scripts/registry.py
python3 scripts/geocode.py
python3 scripts/build.py
python3 scripts/validate.py build/site

unset OPENALEX_API_KEY
unset GEOCODER_CONTACT
```

Jalankan situs melalui HTTP:

```bash
python3 -m http.server 8765 --bind 127.0.0.1 --directory build/site
```

Buka `http://127.0.0.1:8765`. Jangan membuka `index.html` melalui `file://`, karena browser harus memuat snapshot JSON.

## Koreksi lokasi

Tambahkan koreksi yang telah diverifikasi ke `config/location-overrides.json`, menggunakan OpenAlex organization ID sebagai kunci:

```json
{
  "P123456789": {
    "city": "Padang",
    "province": "Sumatera Barat",
    "latitude": -0.9471,
    "longitude": 100.4172,
    "reason": "Alamat resmi penerbit diverifikasi pada situs organisasi"
  }
}
```

Override mengalahkan metadata dan geocoding otomatis. `data/geocode-cache.json` menyimpan respons geocoder agar refresh berikutnya tidak mengulang permintaan. Tinjau `build/site/data/location-audit.json` untuk organisasi ambigu, gagal dipetakan, atau tidak ditemukan.

## Pengujian

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/*.test.mjs
```

## Keamanan dan batasan

- Simpan `OPENALEX_API_KEY` sebagai environment variable lokal atau GitHub Actions secret.
- Rotasi key yang pernah dibagikan melalui saluran komunikasi.
- Lokasi adalah kota organisasi penerbit, bukan lokasi penulis atau kantor redaksi.
- Geocoding otomatis dapat salah. Hasil yang meragukan harus dikoreksi melalui override.
- Jurnal tanpa lokasi tetap masuk total nasional dan ditampilkan sebagai belum terpetakan.
- Workflow mingguan membangun artifact tetapi tidak melakukan commit, push, atau deployment.

`registry.py` menyimpan data DOAJ dan Crossref di `data/registry-cache.json` selama 30 hari (`REGISTRY_MAX_AGE_DAYS`); pakai `--refresh` untuk mengunduh ulang. Untuk jurnal yang belum berlokasi, skrip ini juga mencari member Crossref pendaftar DOI-nya (maksimal `CROSSREF_MAX_MEMBER_LOOKUPS`, bawaan 10000, per jalan; dilanjutkan pada jalan berikutnya) beserta alamatnya. Perubahan judul tercatat di `build/site/data/title-audit.json`.

Lihat [metodologi](docs/METHODOLOGY.md) dan [operasi](docs/OPERATIONS.md) untuk rincian.

