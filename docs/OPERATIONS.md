# Operasi dan pemulihan

## Refresh mingguan

Workflow `refresh.yml` dapat dijalankan terjadwal atau manual. Konfigurasikan:

- secret `OPENALEX_API_KEY`;
- variable `GEOCODER_CONTACT`.

Workflow mengunggah `build/site` sebagai artifact. Ia sengaja tidak melakukan deployment atau commit karena repository tujuan belum ditentukan.
Cache GitHub Actions mempertahankan hasil geocoding antar-run. Workflow membatasi 1.500 kueri geocoding baru per refresh agar mematuhi laju layanan dan batas waktu; organisasi yang tersisa tetap berstatus belum terpetakan dan diproses pada refresh berikutnya.

## Perlindungan snapshot

- Koleksi dan cache ditulis melalui file sementara lalu diganti secara atomik.
- Build baru mengganti `build/site` setelah snapshot selesai dibuat.
- Penurunan total jurnal lebih dari 10% menghentikan build bila situs sebelumnya tersedia.
- Validator memeriksa ID ganda, rekonsiliasi total, koordinat, file wajib, pola credential, dan remote API fetch pada frontend.

Jika suatu tahap gagal, periksa pesan ringkas tanpa menyalin environment variable atau request header ke issue/log publik. Jalankan ulang setelah sumber masalah diperbaiki; situs sebelumnya tetap menjadi versi yang digunakan.

## Audit lokasi

Periksa `build/site/data/location-audit.json`. Untuk salah lokasi, verifikasi alamat organisasi dari sumber resmi lalu tambahkan override beserta alasan. Untuk hasil gagal, jangan membuat koordinat perkiraan tanpa sumber.

