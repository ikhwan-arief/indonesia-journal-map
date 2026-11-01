import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build import build_site, build_snapshot, corrected_title, diamond_status, publisher_type, type_from_name
from collect import Client, collect, compact_source, primary_field
from common import PipelineError
from geocode import (InstitutionIndex, KnownCities, from_nominatim, member_place, from_ror, name_key, normalize_city, normalize_province,
                     registrable_domain, resolve, resolve_journals)
from validate import validate_snapshot
from wilayah import Gazetteer, clean, simplify
from validate import validate_site


class FakeClient:
    def ror(self, value):
        return None

    def nominatim(self, value):
        return [{"lat": "-0.95", "lon": "100.35", "address": {"country_code": "id", "city": "Kota Padang", "state": "Sumatera Barat"}}]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.source = {"country": {"code": "ID", "name": "Indonesia", "openalex_id": "https://openalex.org/countries/ID"},
                       "organizations": {"P1": {"id": "P1", "name": "Universitas Contoh", "geo": {}, "ror": None}},
                       "journals": [{"id": "S1", "name": "Jurnal A", "issn": [], "issn_l": None, "is_oa": True,
                                     "is_in_doaj": False, "works_count": 10, "publisher_id": "P1", "publisher_name": "Universitas Contoh"},
                                    {"id": "S2", "name": "Jurnal B", "issn": [], "issn_l": None, "is_oa": None,
                                     "is_in_doaj": None, "works_count": 2, "publisher_id": "P2", "publisher_name": "Tanpa Lokasi"}]}

    def test_source_compaction_preserves_requested_fields(self):
        row = compact_source({"id": "https://openalex.org/S7", "display_name": "J", "type": "journal", "country_code": "ID",
                              "issn": ["1234-5678"], "issn_l": "1234-5678", "host_organization": "https://openalex.org/P4"})
        self.assertEqual((row["id"], row["publisher_id"], row["issn_l"]), ("S7", "P4", "1234-5678"))

    def test_collect_sends_each_id_type_to_its_own_endpoint(self):
        country = {"code": "ID", "name": "Indonesia"}
        sources = [{"id": "https://openalex.org/S1", "type": "journal", "country_code": "ID",
                    "host_organization": "https://openalex.org/P1"},
                   {"id": "https://openalex.org/S2", "type": "journal", "country_code": "ID",
                    "host_organization": "https://openalex.org/I2"}]
        geo = {"city": "Padang", "region": "West Sumatra", "country_code": "ID", "latitude": -0.95, "longitude": 100.35}

        def get(path, params=None):
            ids = (params or {}).get("filter", "").removeprefix("ids.openalex:").split("|")
            if path == "/countries/ID":
                return {"id": "https://openalex.org/countries/ID", "display_name": "Indonesia"}
            if path == "/publishers":
                self.assertTrue(all(value.startswith("P") for value in ids), ids)
                return {"results": [{"id": "https://openalex.org/P1", "display_name": "Pub", "ids": {"ror": "https://ror.org/x"},
                                     "roles": [{"role": "institution", "id": "https://openalex.org/I9"}]}]}
            if path == "/institutions":
                self.assertTrue(all(value.startswith("I") for value in ids), ids)
                return {"results": [{"id": "https://openalex.org/" + value, "display_name": value, "geo": geo} for value in ids]}
            raise AssertionError(path)

        client = Client("not-a-real-key", interval=0)
        client.get = get
        client.pages = lambda path, params: (sources, 2)
        result = collect(client, country)
        self.assertEqual(result["organizations"]["P1"]["ror"], "https://ror.org/x")
        self.assertEqual(result["organizations"]["P1"]["institution_id"], "I9")
        self.assertEqual(result["organizations"]["I2"]["geo"]["city"], "Padang")

    def test_ror_v2_location_includes_province(self):
        payload = {"locations": [{"geonames_details": {"country_code": "ID", "country_subdivision_name": "West Java",
                                                       "lat": -6.92, "lng": 107.6, "name": "Bandung"}}]}
        self.assertEqual((from_ror(payload)["city"], from_ror(payload)["province"]), ("Bandung", "Jawa Barat"))

    def test_province_names_are_canonical(self):
        self.assertEqual(normalize_province("Bangka–Belitung Islands"), "Kepulauan Bangka Belitung")
        self.assertEqual(normalize_province("Daerah Istimewa Yogyakarta"), "DI Yogyakarta")
        self.assertEqual(normalize_province("Provinsi Jawa Barat"), "Jawa Barat")
        self.assertIsNone(normalize_province(None))

    def test_cursor_pagination_deduplicates(self):
        client = Client("not-a-real-key", interval=0)
        pages = [{"meta": {"count": 2, "next_cursor": "two"}, "results": [{"id": "S1"}]},
                 {"meta": {"count": 2, "next_cursor": None}, "results": [{"id": "S1"}, {"id": "S2"}]}]
        client.get = mock.Mock(side_effect=pages)
        rows, expected = client.pages("/sources", {"filter": "country_code:ID,type:journal"})
        self.assertEqual(([row["id"] for row in rows], expected), (["S1", "S2"], 2))

    def test_geocoder_rejects_non_indonesia_and_ambiguity(self):
        self.assertIsNone(from_nominatim([{"lat": "1", "lon": "103", "address": {"country_code": "sg", "city": "Singapore"}}]))
        rows = [{"lat": "-0.9", "lon": "100.3", "address": {"country_code": "id", "city": "Padang"}},
                {"lat": "-6.2", "lon": "106.8", "address": {"country_code": "id", "city": "Jakarta"}}]
        self.assertIsNone(from_nominatim(rows))

    def test_override_wins_and_cache_is_reused(self):
        override = {"P1": {"city": "Kota Padang", "province": "Sumatera Barat", "latitude": -0.95, "longitude": 100.35, "reason": "verified"}}
        client = FakeClient()
        client.nominatim = mock.Mock(side_effect=AssertionError("must not geocode override"))
        resolved, _, _ = resolve(self.source, override, {}, client)
        self.assertEqual((resolved["P1"]["city"], resolved["P1"]["source"]), ("Padang", "override"))

    def test_city_normalization_and_unmapped_accounting(self):
        self.assertEqual(normalize_city("Kota Padang"), "Padang")
        self.assertEqual(normalize_city("Jambi City"), "Jambi")
        self.assertEqual(normalize_city("Sidoarjo Regency"), "Sidoarjo")
        locations = {"locations": {"P1": {"city": "Padang", "province": "Sumatera Barat", "latitude": -0.95,
                                                  "longitude": 100.35, "source": "geocoded"}}}
        snapshot = build_snapshot(self.source, locations)
        self.assertEqual(snapshot["summary"]["mapped_journals"], 1)
        self.assertEqual(snapshot["summary"]["unmapped_journals"], 1)
        validate_snapshot(snapshot)

    def test_city_without_province_joins_same_named_city(self):
        self.source["organizations"]["P2"] = {"id": "P2", "name": "Tanpa Lokasi", "geo": {}, "ror": None}
        locations = {"locations": {
            "P1": {"city": "Padang", "province": "Sumatera Barat", "latitude": -0.95, "longitude": 100.35, "source": "ror"},
            "P2": {"city": "Padang", "province": None, "latitude": -0.94, "longitude": 100.36, "source": "openalex"}}}
        snapshot = build_snapshot(self.source, locations)
        self.assertEqual([(city["name"], city["journal_count"]) for city in snapshot["cities"]], [("Padang", 2)])

    def test_duplicate_journal_is_rejected(self):
        self.source["journals"].append(dict(self.source["journals"][0]))
        with self.assertRaises(PipelineError):
            build_snapshot(self.source, {"locations": {}})

    def test_built_site_passes_public_validation(self):
        locations = {"locations": {"P1": {"city": "Padang", "province": "Sumatera Barat", "latitude": -0.95,
                                                  "longitude": 100.35, "source": "override"}}, "audit": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path, locations_path, output = root / "input.json", root / "locations.json", root / "site"
            input_path.write_text(json.dumps(self.source), encoding="utf-8")
            locations_path.write_text(json.dumps(locations), encoding="utf-8")
            build_site(input_path, locations_path, output)
            validate_site(output)


class EnrichmentTests(unittest.TestCase):
    registry = {"doaj": {"records": {"0854-0268": {"title": "Jurnal Ushuluddin", "publisher": "UIN Sumatera Utara",
                                                   "country": "Indonesia", "has_apc": False, "has_other_fees": False},
                                     "1111-1111": {"title": "Jurnal Teknik", "has_apc": True, "has_other_fees": False}}},
                "crossref": {"records": {"2222-2222": {"title": "Journal of Physics Education", "publisher": "Universitas Jember"}}}}

    def test_primary_field_sums_topic_counts(self):
        topic = lambda field, domain, count: {"count": count, "field": {"id": f"https://openalex.org/fields/{field}", "display_name": f"F{field}"},
                                              "domain": {"id": f"https://openalex.org/domains/{domain}", "display_name": f"D{domain}"}}
        field, domain = primary_field({"topics": [topic(33, 2, 10), topic(22, 3, 8), topic(22, 3, 7)]})
        self.assertEqual((field["id"], domain["id"]), ("22", "3"))
        self.assertEqual(primary_field({"topics": []}), (None, None))

    def test_title_correction_replaces_publisher_statement_only(self):
        bad = {"name": '"Diterbitkan Oleh Fakultas Ushuluddin Dan Studi Islam Universitas Islam Negeri Sumatera Utara Medan"',
               "issn": ["0854-0268", "2685-6042"], "issn_l": "0854-0268"}
        self.assertEqual(corrected_title(bad, self.registry), ("Jurnal Ushuluddin", "doaj"))
        close = {"name": "JURNAL TEKNIK", "issn": ["1111-1111"]}
        self.assertEqual(corrected_title(close, self.registry), ("JURNAL TEKNIK", "openalex"))
        renamed = {"name": "Jurnal Fisika", "issn": ["2222-2222"]}
        self.assertEqual(corrected_title(renamed, self.registry), ("Jurnal Fisika", "openalex"))  # Crossref alone: keep
        unknown = {"name": "Anything", "issn": ["9999-9999"]}
        self.assertEqual(corrected_title(unknown, self.registry), ("Anything", "openalex"))

    def test_diamond_requires_doaj_record(self):
        self.assertIs(diamond_status({"issn": ["0854-0268"]}, self.registry), True)
        self.assertIs(diamond_status({"issn": ["1111-1111"]}, self.registry), False)
        self.assertIsNone(diamond_status({"issn": ["2222-2222"]}, self.registry))

    def test_domain_and_name_matching(self):
        self.assertEqual(registrable_domain("https://jurnal.unej.ac.id/index.php/x"), "unej.ac.id")
        self.assertEqual(registrable_domain("http://www.example.com/a"), "example.com")
        self.assertEqual(name_key("Universitas Jember"), name_key("University of Jember"))
        geo = lambda lat, lon, city: {"city": city, "region": "East Java", "country_code": "ID", "latitude": lat, "longitude": lon}
        index = InstitutionIndex([{"id": "I1", "name": "University of Jember", "homepage_url": "https://unej.ac.id", "geo": geo(-8.16, 113.72, "Jember")},
                                  {"id": "I2", "name": "A", "homepage_url": "https://shared.ac.id", "geo": geo(-7.2, 112.7, "Surabaya")},
                                  {"id": "I3", "name": "B", "homepage_url": "https://shared.ac.id/b", "geo": geo(-7.9, 112.6, "Malang")}])
        self.assertEqual(index.by_homepage("https://jurnal.unej.ac.id/x")[0]["city"], "Jember")
        self.assertEqual(index.by_homepage("https://shared.ac.id/j"), (None, "ambiguous_domain"))
        self.assertEqual(index.by_publisher_name("Physics Department, Faculty of Science, Universitas Jember")["city"], "Jember")

    def test_journal_level_fallback_maps_journal_without_publisher(self):
        source = {"country": {"code": "ID"}, "organizations": {},
                  "institutions_index": [{"id": "I1", "name": "University of Jember", "homepage_url": "https://unej.ac.id",
                                          "geo": {"city": "Jember", "region": "East Java", "country_code": "ID",
                                                  "latitude": -8.16, "longitude": 113.72}}],
                  "journals": [{"id": "S1", "name": "J1", "issn": [], "publisher_id": "", "homepage_url": "https://jurnal.unej.ac.id/j1"},
                               {"id": "S2", "name": "J2", "issn": ["2222-2222"], "publisher_id": "", "homepage_url": None},
                               {"id": "S3", "name": "J3", "issn": [], "publisher_id": "", "homepage_url": None}]}
        client = FakeClient()
        client.nominatim = mock.Mock(side_effect=AssertionError("name match must avoid geocoding"))
        places, audit = resolve_journals(source, {}, self.registry, {}, client, {"new": 0, "max": None})
        self.assertEqual(set(places), {"S1", "S2"})
        self.assertEqual((places["S1"]["source"], places["S2"]["source"]), ("homepage-domain", "registry-publisher"))
        snapshot = build_snapshot(source, {"locations": {}, "journal_locations": places}, self.registry)
        self.assertEqual(snapshot["summary"]["mapped_journals"], 2)
        validate_snapshot(dict(snapshot, journals=[dict(j, id=f"S{i}") for i, j in enumerate(snapshot["journals"], 1)]))

        self.assertEqual(snapshot["journals"][0]["publisher"]["type"], "education")  # unej.ac.id
        self.assertEqual(snapshot["summary"]["doaj_journals"], 0)

    def test_crossref_member_address(self):
        padang = {"city": "Padang", "province": "Sumatera Barat", "latitude": -0.95, "longitude": 100.35, "source": "openalex"}
        known = KnownCities([padang])
        client = FakeClient()
        client.nominatim = mock.Mock(side_effect=AssertionError("known city must not be geocoded"))
        budget = {"new": 0, "max": None}
        for location in ("Padang, West Sumatra, Indonesia", "Jl. Limau Manis No. 1, Kota Padang, Sumatera Barat, Indonesia",
                         "Padang, Indonesia"):
            place = member_place({"location": location}, known, {}, client, budget, None)
            self.assertEqual((place["city"], place["source"]), ("Padang", "crossref-member"), location)
        self.assertIsNone(member_place({"location": "Indonesia"}, known, {}, client, budget, None))
        self.assertIsNone(member_place({"location": "West Java, Indonesia"}, known, {}, client, budget, None))
        jakarta = {"city": "Jakarta", "province": "DKI Jakarta", "latitude": -6.2, "longitude": 106.8, "source": "openalex"}
        mataram = {"city": "Mataram", "province": "Nusa Tenggara Barat", "latitude": -8.58, "longitude": 116.1, "source": "openalex"}
        known = KnownCities([padang, jakarta, jakarta, mataram])
        for location, city in (("Jakarta, Indonesia", "Jakarta"), ("Jakarta Timur, Indonesia", "Jakarta"),
                               ("Jakarta Selatan, DKI Jakarta, Indonesia", "Jakarta"), ("Mataram, NTB, Indonesia", "Mataram"),
                               ("Palmerah, https://example.org/x, Padang, Indonesia", "Padang")):
            self.assertEqual(member_place({"location": location}, known, {}, client, budget, None)["city"], city, location)
        # Unknown city: geocode once, and reject a same-named place in another province.
        client.nominatim = mock.Mock(return_value=[{"lat": "-7.8", "lon": "110.4", "address": {
            "country_code": "id", "city": "Bantul", "state": "Daerah Istimewa Yogyakarta"}}])
        place = member_place({"location": "Bantul, Yogyakarta, Indonesia"}, known, {}, client, budget, None)
        self.assertEqual(place["city"], "Bantul")
        self.assertIsNone(member_place({"location": "Bantul, East Java, Indonesia"}, known, {}, client, budget, None))

    def test_official_areas(self):
        square = lambda lat, lng, d=0.2: [[lat - d, lng - d], [lat - d, lng + d], [lat + d, lng + d], [lat + d, lng - d], [lat - d, lng - d]]
        data = {"source": "test", "provinces": {"32": "Jawa Barat", "33": "Jawa Tengah", "31": "DKI Jakarta"},
                "regencies": {"32.71": {"name": "Kota Bogor", "capital": "Bogor", "lat": -6.59, "lng": 106.79, "rings": [square(-6.59, 106.79, 0.05)]},
                              "32.01": {"name": "Kabupaten Bogor", "capital": "Cibinong", "lat": -6.48, "lng": 106.82, "rings": [square(-6.5, 106.6)]},
                              "33.02": {"name": "Kabupaten Banyumas", "capital": "Purwokerto", "lat": -7.42, "lng": 109.23, "rings": [square(-7.42, 109.23)]},
                              "31.74": {"name": "Kota Administrasi Jakarta Selatan", "capital": "Kebayoran Baru", "lat": -6.25, "lng": 106.81, "rings": [square(-6.25, 106.81, 0.05)]}},
                "districts": {"32.01.01": "Dramaga", "33.02.01": "Purwokerto Utara", "33.02.02": "Purwokerto Selatan"},
                "villages": {"31.74.01.1001": "Palmerah"}}
        g = Gazetteer(data)
        city = lambda *args: (lambda c: (c[0]["city"], c[1]) if c[0] else None)(g.locate(*args))
        self.assertEqual(city("Kabupaten Bogor"), ("Bogor", "city"))  # kota and kabupaten of one name are one city
        self.assertEqual(g.locate("Bogor")[0]["latitude"], -6.59)    # placed at the kota's centre
        self.assertEqual(city("Cibinong"), ("Bogor", "capital"))
        self.assertEqual(city("Purwokerto", "Jawa Tengah"), ("Banyumas", "capital"))
        self.assertEqual(city("Kec. Dramaga", "Jawa Barat"), ("Bogor", "district"))
        self.assertIsNone(city("Dramaga"))                             # a bare district name needs a province or a point
        self.assertEqual(city("Dramaga", None, -6.55, 106.72), ("Bogor", "district"))
        self.assertEqual(city("South Jakarta"), ("Jakarta", "city"))
        self.assertEqual(city("Palmerah", "DKI Jakarta"), ("Jakarta", "village"))
        self.assertEqual(city("Somewhere", None, -6.45, 106.55), ("Bogor", "boundary"))
        self.assertIsNone(city("Somewhere", None, 0.0, 120.0))
        self.assertEqual(clean("Kota Administrasi Jakarta Selatan"), "jakarta selatan")
        self.assertEqual(clean("South Tangerang City"), "tangerang selatan")
        self.assertEqual(len(simplify(square(0, 0), 0.01)), 5)  # closed rings keep their corners

    def test_publisher_type(self):
        inst = {"I1": {"type": "education"}, "I2": {"type": "government"}, "I3": {"type": "company"},
                "I4": {"type": "facility", "homepage_url": "http://www.lipi.go.id/"}, "I5": {"type": "other"},
                "I6": {"type": "education", "homepage_url": "https://www.unud.ac.id", "name": "Udayana University"}}
        kind = lambda name="", url=None, pid="", org=None, place=None, overrides=None: publisher_type(
            {"publisher_id": pid, "publisher_name": name, "homepage_url": url}, org or {}, place, inst, [], overrides)
        # 1. Official domains come first: journal site, then the institution's site.
        self.assertEqual(kind("PT Media Krafon Sejahtera", "https://jurnal.x.ac.id/j"), ("education", "domain"))
        self.assertEqual(kind("Universitas X", "https://ojs.x.go.id/j"), ("government", "domain"))
        self.assertEqual(kind("", "https://jurnal.sman1.sch.id/j"), ("education", "domain"))
        self.assertEqual(kind("", "https://x.ponpes.id/j"), ("education", "domain"))
        self.assertEqual(kind("", "https://journal.x.edu/j"), ("education", "domain"))
        self.assertEqual(kind("", "https://jurnal.x.desa.id"), ("government", "domain"))
        self.assertEqual(kind("Indonesian Institute of Sciences", "https://jurnalet.com/j", "P2", {"institution_id": "I4"}),
                         ("government", "domain"))  # institution site lipi.go.id beats the journal's .com site
        self.assertEqual(kind("", "https://iain-x.academia.edu"), (None, None))  # hosting platforms are ignored
        # 2. Name rules and OpenAlex types, ahead of commercial domains.
        for name in ("PT Media Krafon Sejahtera", "CV. Pustaka Ilmu", "Penerbit X, CV"):
            self.assertEqual(kind(name, "https://x.com"), ("private", "name"), name)
        for name in ("Rumah Sakit Umum Pusat Dr. Sardjito", "RSUD Dr. Soetomo", "Universitas Airlangga Hospital",
                     "Komisi Pengkajian Majelis Ulama Indonesia Provinsi Jawa Barat"):
            self.assertEqual(kind(name, "https://x.com"), ("government", "name"), name)
        self.assertEqual(kind("STIKES RS Baptis Kediri"), ("education", "name"))
        self.assertEqual(kind("Lembaga Layanan Pendidikan Tinggi Wilayah IV", "https://x.or.id"), ("government", "name"))
        self.assertEqual(kind("Kopertais DKI Jakarta and Banten"), ("government", "name"))
        self.assertEqual(kind("Yayasan Insanulhaq", None, "I2"), ("private", "name"))  # beats OpenAlex "government"
        self.assertEqual(kind("Poltekkes Kemenkes Semarang"), ("education", "name"))
        # The hospital rule outranks the domain of the linked institution (Udayana, .ac.id), not the journal's own site.
        self.assertEqual(kind("Sanglah Hospital - Udayana University", "https://perinasiajournal.id", "P7", {"institution_id": "I6"}),
                         ("government", "name"))
        self.assertEqual(kind("Sanglah Hospital - Udayana University", "https://jurnal.unud.ac.id/x"), ("education", "domain"))
        self.assertEqual(kind("Universitas X", "https://journal.x.com/j"), ("education", "name"))
        self.assertEqual(kind("Akademi Keperawatan X", "https://x.com/j"), ("education", "name"))
        self.assertEqual(kind("Seminari Tinggi X", "https://x.com", "I1"), ("education", "openalex"))
        self.assertEqual(kind("", None, "P1", {"institution_id": "I2"}), ("government", "openalex"))
        self.assertEqual(kind("", None, "I3"), ("private", "openalex"))
        self.assertEqual(kind("", None, "", None, {"institution_id": "I1"}), ("education", "openalex"))
        # 3. Commercial domains, then the remaining keywords.
        self.assertEqual(kind("Pustaka X", "https://journal.x.com/j"), ("private", "domain"))
        self.assertEqual(kind("", "https://x.co.id/j"), ("private", "domain"))
        self.assertEqual(kind("Ministry of Environment and Forestry", "http://forda-mof.org/j", "P3", {"institution_id": "I5"}),
                         ("government", "name"))
        self.assertEqual(kind("", "https://x.or.id/j"), ("private", "domain"))
        self.assertEqual(kind("", "https://x.org"), (None, None))
        for name, expected in [("Badan Penerbit Universitas Diponegoro", "education"), ("Kementerian Kesehatan RI", "government"),
                               ("Indonesian Agency for Agricultural Research and Development", "government"),
                               ("National Nuclear Energy Agency", "government"), ("Bank Indonesia", "government"),
                               ("State Islamic Institute of Fattahul Muluk", "education"), ("UIR Press", "education"),
                               ("Yayasan Pendidikan Cendekia", "private"), ("CV. Media Ilmu", "private"), ("Jurnal Sains", None),
                               ("Lasigo Akademia Indonesia", None)]:
            self.assertEqual(type_from_name(name), expected, name)
        # 0. Manual overrides win over everything.
        overrides = {"names": {"Indonesian Institute for Counseling, Education and Therapy": "private"}, "ids": {"P9": "government"}}
        self.assertEqual(kind("Indonesian Institute for Counseling, Education and Therapy (IICET)", "https://x.ac.id", "P1",
                              {"institution_id": "I5"}, None, overrides), ("private", "manual"))
        self.assertEqual(kind("", None, "P9", None, None, overrides), ("government", "manual"))

if __name__ == "__main__":
    unittest.main()
