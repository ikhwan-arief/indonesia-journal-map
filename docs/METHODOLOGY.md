# Metodologi

## Populasi

Kolektor meminta endpoint `/sources` dengan filter berikut:

```text
country_code:ID,type:journal
```

Setiap OpenAlex Source ID dihitung satu kali. `works_count` hanya menjadi metadata tabel dan tidak menentukan ukuran bubble.

## Resolusi lokasi

Lokasi dipilih dengan urutan koreksi manual, metadata OpenAlex, metadata ROR, lalu geocoding otomatis. Koreksi manual ditempatkan pertama karena berfungsi memperbaiki metadata yang diketahui salah. Hasil geocoding diterima hanya bila kode negaranya `ID`, koordinat berada dalam batas geografis Indonesia yang dikonfigurasi, dan beberapa kandidat—jika ada—menunjuk kota serta provinsi yang sama.

Nama `Kota X` dan `X` dinormalisasi menjadi kota yang sama. Nama provinsi dari OpenAlex dan ROR (bahasa Inggris, misalnya `West Java`) serta dari Nominatim (bahasa Indonesia) diseragamkan ke nama resmi, misalnya `Jawa Barat`. Lokasi tanpa provinsi memakai provinsi kota bernama sama bila hanya ada satu kemungkinan; bila ada lebih dari satu, kota tetap terpisah dan perlu override. Nama yang sekadar mirip tidak digabung. Jurnal tanpa lokasi yang dapat diterima diberi status `unmapped` dan tidak ditempatkan pada titik nasional buatan.

## Kabupaten/kota resmi

Semua lokasi akhirnya diseragamkan ke kabupaten/kota resmi menurut kode wilayah Kemendagri (Kepmendagri No 300.2.2-2138 Tahun 2025, lewat data terbuka github.com/cahyadsn/wilayah, lisensi MIT). Data ini memuat 38 provinsi, 514 kabupaten/kota (dengan koordinat ibu kota dan batas wilayah), 7.285 kecamatan, dan 83.762 kelurahan/desa, dan disimpan di `data/wilayah.json` (dibuat ulang dengan `python3 scripts/wilayah.py`). Urutan pencocokan:

1. Nama kabupaten/kota, termasuk bentuk Inggris ("South Tangerang" = Tangerang Selatan).
2. Nama ibu kota kabupaten (Purwokerto → Banyumas, Singaraja → Buleleng, Cibinong → Bogor).
3. Nama kecamatan atau kelurahan/desa, bila unik di provinsinya atau dekat titik koordinatnya (Tembalang → Semarang, Jatinangor → Sumedang, Ciputat → Tangerang Selatan).
4. Batas wilayah yang memuat titik koordinat.

Kota dan kabupaten yang bernama sama dalam satu provinsi (Kota/Kabupaten Bogor, Malang, Semarang, dll.) ditampilkan sebagai satu kota di titik pusat kotanya, dan lima kota administrasi DKI Jakarta ditampilkan sebagai Jakarta. Lokasi institusi yang salah di sumbernya (ROR/OpenAlex memilih desa lain bernama sama) dikoreksi di `config/location-overrides.json` beserta catatannya. Hasil pencocokan tercatat di `location-audit.json` (`official_areas`).

## Pemulihan lokasi tingkat jurnal

Jurnal yang penerbitnya kosong di OpenAlex atau tidak dapat dilokasikan diberi lokasi cadangan secara berurutan:

1. **Domain situs jurnal** (`homepage-domain`): domain terdaftar (mis. `unej.ac.id`) harus cocok dengan tepat satu institusi Indonesia di OpenAlex yang memiliki koordinat. Domain bersama (platform hosting) dan domain yang cocok dengan beberapa institusi diabaikan.
2. **Nama penerbit dari registri ISSN** (`registry-publisher`): nama penerbit di DOAJ (hanya rekaman berasal Indonesia) atau Crossref dicocokkan dengan nama institusi. Pencocokan memakai kunci kata tanpa urutan yang menyamakan bentuk Indonesia/Inggris (mis. "Universitas Jember" = "University of Jember") dan singkatan UIN/IAIN/STAIN. Bila tidak cocok dan namanya jelas berupa institusi pendidikan, nama tersebut di-geocode dengan aturan penerimaan Nominatim yang sama.
3. **Alamat member Crossref dari DOI** (`crossref-member`): setiap DOI didaftarkan oleh *member* Crossref (biasanya penerbitnya), yang mencatat alamat seperti "Padang, West Sumatra, Indonesia". Member pendaftar DOI jurnal dicari lewat ISSN (`/journals/{issn}/works`), lalu kota dari alamat member dicocokkan dengan kota yang sudah dikenal di provinsi yang sama. Bila tidak ada yang cocok, alamat di-geocode dengan Nominatim, dan hasil yang provinsinya berbeda ditolak. Bagian alamat berupa jalan (Jl., Komplek, nomor) diabaikan, dan alamat yang hanya berisi "Indonesia" tidak dipakai. Hanya member yang alamatnya di Indonesia yang dipakai.

Bila OpenAlex, DOAJ, dan Crossref tidak mencatat nama penerbit sebuah jurnal, nama member Crossref pendaftar DOI-nya dipakai sebagai nama penerbit (`publisher.source = crossref-member`). Nama ini juga ikut dipakai untuk menentukan jenis penerbit.

Jurnal tanpa penerbit, tanpa situs, atau dengan penerbit independen yang tidak tercatat tetap berstatus belum terpetakan. Alasannya tercatat di `location-audit.json`.

## Judul jurnal

Judul OpenAlex diganti dengan judul rekaman ISSN (DOAJ lebih dahulu, lalu Crossref) hanya bila semua ISSN jurnal menunjuk judul yang sama dan judul OpenAlex tampak seperti keterangan penerbit (mis. diawali "Diterbitkan oleh") atau kemiripannya di bawah 0,6. Nama OpenAlex tetap disimpan dan dapat dicari.

## Bidang ilmu

Bidang utama jurnal adalah *field* OpenAlex dengan jumlah karya bertopik terbanyak pada jurnal tersebut, dikelompokkan ke empat *domain* OpenAlex. Jurnal tanpa data topik ditandai tanpa klasifikasi.

## Diamond open access

Jurnal *diamond* adalah jurnal yang tercatat di DOAJ tanpa *article processing charge* dan tanpa biaya lain bagi penulis, dicocokkan dengan OpenAlex melalui ISSN. Jurnal yang tidak ada di DOAJ tidak dinilai (bukan berarti berbayar).

## Terindeks DOAJ

Jurnal dihitung terindeks DOAJ bila OpenAlex menandainya `is_in_doaj`, atau bila salah satu ISSN-nya ada di daftar jurnal DOAJ. Semua jurnal *diamond* termasuk di dalamnya.

## Jenis penerbit

Kategori: Education, Government agency, Private, Other. Ekstensi domain menjadi dasar utama. Domain resmi Indonesia (`.ac.id`, `.sch.id`, `.ponpes.id`, `.go.id`, `.mil.id`, `.desa.id`) hanya diberikan kepada lembaga yang berhak, sedangkan `.com`/`.co.id` terbuka untuk siapa saja. Aturan dicek berurutan; aturan pertama yang cocok dipakai:

1. Koreksi manual di `config/publisher-type-overrides.json` (nama atau ID OpenAlex penerbit).
2. Domain situs jurnal: `.ac.id`, `.sch.id`, `.ponpes.id`, `.edu` → Education; `.go.id`, `.mil.id`, `.desa.id` → Government agency.
3. Nama penerbit memuat PT (Perseroan Terbatas) atau CV (Commanditaire Vennootschap) → Private.
4. Nama *Rumah Sakit*/*RSUP*/*RSUD*/*Hospital* (kecuali STIKES/akademi/asosiasi yang memakai nama rumah sakit), *Majelis Ulama Indonesia*, *LLDIKTI*/*Kopertis*/*Kopertais* → Government agency.
5. Domain situs institusi OpenAlex yang tertaut ke penerbit, dengan pembagian seperti langkah 2.
6. Nama penerbit atau institusi tertaut memuat *University*/*Universitas*/*Akademi* → Education.
7. Nama *Yayasan*, *Perkumpulan*, *Asosiasi*, *Ikatan*, *Himpunan*, *Konsorsium* → Private.
8. Tipe institusi OpenAlex: `education` → Education; `government`/`funder` → Government agency; `company`/`nonprofit` → Private.
9. Domain komersial situs jurnal atau institusi: `.com`, `.co.id`, `.biz.id` → Private.
10. Kata kunci nama lain: *Politeknik*, *Poltekkes*, *Sekolah Tinggi*, *Sekolah* → Education; *Kementerian*, *Badan*, *Agency*, *Balai*, *Bank Indonesia* → Government agency; *Publisher*, *Penerbit* → Private.
11. Sinyal lemah: tipe OpenAlex `facility`/`healthcare`/`archive`/`other` → Other; domain `.or.id` → Private; kata umum seperti *Press*, *Institute*, *Research* → Private.

Aturan nama memakai nama penerbit dari OpenAlex; nama dari DOAJ/Crossref hanya dipakai bila OpenAlex tidak mencatat penerbit. Hosting gratis dan portal (Blogspot, WordPress, Academia.edu, GitHub Pages, Neliti, dll.) tidak dihitung sebagai domain penerbit. Universitas swasta dihitung sebagai Education. `publisher.type_source` (`manual`, `domain`, `name`, `openalex`) mencatat dasar penentuan.

## Interpretasi

Bubble mengagregasi jurnal berdasarkan kota organisasi penerbit. Luas lingkaran menggunakan skala akar kuadrat sehingga luas visual sebanding dengan jumlah jurnal. Hasil tidak menyatakan jumlah artikel yang ditulis di suatu kota, kualitas jurnal, akreditasi, maupun lokasi fisik kantor redaksi.

Peta dasar memakai batas negara dan provinsi Natural Earth 1:10m (domain publik) yang disederhanakan oleh `scripts/basemap.py` menjadi `web/assets/basemap.json`. Natural Earth masih memakai 33 provinsi, sehingga pemekaran provinsi terbaru belum tampil sebagai batas terpisah. Proyeksinya equirectangular dengan skala sama pada kedua sumbu; koordinat bubble menggunakan lintang dan bujur hasil pipeline. Metadata OpenAlex tersedia sebagai CC0.

