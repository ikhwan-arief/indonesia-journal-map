#!/usr/bin/env python3
"""Resolve publisher cities, using verified metadata before cached Nominatim geocoding."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import ROOT, PipelineError, atomic_json, read_json

INDONESIA_BOUNDS = (-11.5, 6.5, 94.0, 142.0)


def valid_coordinates(latitude, longitude):
    return (isinstance(latitude, (int, float)) and isinstance(longitude, (int, float))
            and INDONESIA_BOUNDS[0] <= latitude <= INDONESIA_BOUNDS[1]
            and INDONESIA_BOUNDS[2] <= longitude <= INDONESIA_BOUNDS[3])


def normalize_text(value):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_city(value):
    name = re.sub(r"^(kota|kabupaten)\s+", "", str(value or "").strip(), flags=re.I)
    name = re.sub(r"\s+(city|regency)$", "", name, flags=re.I)
    return " ".join(part.capitalize() for part in name.split())


# Official Indonesian province names, keyed by normalized English (GeoNames/ROR/OpenAlex) and Indonesian variants.
PROVINCES = {
    "Aceh": ["aceh", "nanggroe aceh darussalam"],
    "Sumatera Utara": ["north sumatra", "sumatera utara"],
    "Sumatera Barat": ["west sumatra", "sumatera barat"],
    "Riau": ["riau"],
    "Kepulauan Riau": ["riau islands", "kepulauan riau"],
    "Jambi": ["jambi"],
    "Sumatera Selatan": ["south sumatra", "sumatera selatan"],
    "Kepulauan Bangka Belitung": ["bangka belitung islands", "bangkabelitung islands", "bangka belitung", "kepulauan bangka belitung"],
    "Bengkulu": ["bengkulu"],
    "Lampung": ["lampung"],
    "DKI Jakarta": ["jakarta", "dki jakarta", "daerah khusus ibukota jakarta", "daerah khusus jakarta",
                    "special capital region of jakarta"],
    "Jawa Barat": ["west java", "jawa barat"],
    "Banten": ["banten"],
    "Jawa Tengah": ["central java", "jawa tengah"],
    "DI Yogyakarta": ["yogyakarta", "di yogyakarta", "daerah istimewa yogyakarta", "special region of yogyakarta"],
    "Jawa Timur": ["east java", "jawa timur"],
    "Bali": ["bali"],
    "Nusa Tenggara Barat": ["west nusa tenggara", "nusa tenggara barat"],
    "Nusa Tenggara Timur": ["east nusa tenggara", "nusa tenggara timur"],
    "Kalimantan Barat": ["west kalimantan", "kalimantan barat"],
    "Kalimantan Tengah": ["central kalimantan", "kalimantan tengah"],
    "Kalimantan Selatan": ["south kalimantan", "kalimantan selatan"],
    "Kalimantan Timur": ["east kalimantan", "kalimantan timur"],
    "Kalimantan Utara": ["north kalimantan", "kalimantan utara"],
    "Sulawesi Utara": ["north sulawesi", "sulawesi utara"],
    "Gorontalo": ["gorontalo"],
    "Sulawesi Tengah": ["central sulawesi", "sulawesi tengah"],
    "Sulawesi Barat": ["west sulawesi", "sulawesi barat"],
    "Sulawesi Selatan": ["south sulawesi", "sulawesi selatan"],
    "Sulawesi Tenggara": ["southeast sulawesi", "south east sulawesi", "sulawesi tenggara"],
    "Maluku": ["maluku"],
    "Maluku Utara": ["north maluku", "maluku utara"],
    "Papua": ["papua"],
    "Papua Barat": ["west papua", "papua barat"],
    "Papua Barat Daya": ["southwest papua", "south west papua", "papua barat daya"],
    "Papua Pegunungan": ["highland papua", "papua pegunungan"],
    "Papua Selatan": ["south papua", "papua selatan"],
    "Papua Tengah": ["central papua", "papua tengah"],
}
# Common Indonesian abbreviations, as typed in publisher addresses ("Mataram, NTB").
PROVINCE_ABBREVIATIONS = {"nad": "Aceh", "sumut": "Sumatera Utara", "sumbar": "Sumatera Barat", "kepri": "Kepulauan Riau",
                          "sumsel": "Sumatera Selatan", "babel": "Kepulauan Bangka Belitung", "jabar": "Jawa Barat",
                          "jateng": "Jawa Tengah", "jatim": "Jawa Timur", "diy": "DI Yogyakarta", "ntb": "Nusa Tenggara Barat",
                          "ntt": "Nusa Tenggara Timur", "kalbar": "Kalimantan Barat", "kalteng": "Kalimantan Tengah",
                          "kalsel": "Kalimantan Selatan", "kaltim": "Kalimantan Timur", "kaltara": "Kalimantan Utara",
                          "sulut": "Sulawesi Utara", "sulteng": "Sulawesi Tengah", "sulsel": "Sulawesi Selatan",
                          "sultra": "Sulawesi Tenggara", "sulbar": "Sulawesi Barat", "malut": "Maluku Utara"}
PROVINCE_ALIASES = {alias: name for name, aliases in PROVINCES.items() for alias in aliases}
PROVINCE_ALIASES.update({key: value for key, value in PROVINCE_ABBREVIATIONS.items() if value in PROVINCES})


def normalize_province(value):
    text = str(value or "").strip()
    key = re.sub(r"^provinsi\s+", "", normalize_text(text))
    return PROVINCE_ALIASES.get(key, text) or None


def location(city, province, latitude, longitude, source):
    if not city or not valid_coordinates(latitude, longitude):
        return None
    return {"city": normalize_city(city), "province": normalize_province(province),
            "latitude": float(latitude), "longitude": float(longitude), "source": source}


class PublicClient:
    def __init__(self, contact="", timeout=30):
        self.contact, self.timeout, self.last_geocode = contact, timeout, 0.0

    def get(self, url, geocoder=False):
        if geocoder:
            time.sleep(max(0, self.last_geocode + 1.05 - time.monotonic()))
            self.last_geocode = time.monotonic()
        headers = {"User-Agent": "IndonesiaJournalMap/1.0" + (f" ({self.contact})" if self.contact else "")}
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise PipelineError(f"Location service returned HTTP {error.code}; previous data retained.") from None
                time.sleep(min(16, 2 ** attempt))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                if attempt == 3:
                    raise PipelineError("Location service request failed; previous data retained.") from None
                time.sleep(min(16, 2 ** attempt))
        return None

    def ror(self, ror_url):
        ror_id = str(ror_url or "").rstrip("/").rsplit("/", 1)[-1]
        return self.get("https://api.ror.org/v2/organizations/" + urllib.parse.quote(ror_id)) if ror_id else None

    def nominatim(self, name):
        if not self.contact:
            raise PipelineError("GEOCODER_CONTACT is required before uncached automatic geocoding.")
        params = {"q": f"{name}, Indonesia", "format": "jsonv2", "addressdetails": 1,
                  "countrycodes": "id", "limit": 3, "email": self.contact}
        return self.get("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params), geocoder=True)


def from_openalex(org):
    geo = org.get("geo") or {}
    if str(geo.get("country_code") or "ID").upper() != "ID":
        return None
    return location(geo.get("city"), geo.get("region"), geo.get("latitude"), geo.get("longitude"), "openalex")


def from_ror(payload):
    if not payload:
        return None
    locations = payload.get("locations") or []
    details = (locations[0].get("geonames_details") or {}) if locations else {}
    country = details.get("country_code") or (payload.get("country") or {}).get("country_code")
    if str(country or "").upper() != "ID":
        return None
    return location(details.get("name") or details.get("city"), details.get("country_subdivision_name") or details.get("admin1_name"),
                    details.get("lat"), details.get("lng"), "ror")


def from_override(value):
    if not isinstance(value, dict):
        return None
    return location(value.get("city"), value.get("province"), value.get("latitude"), value.get("longitude"), "override")


def from_nominatim(results):
    accepted = []
    for row in results or []:
        address = row.get("address") or {}
        if str(address.get("country_code") or "").upper() != "ID":
            continue
        city = next((address.get(key) for key in ("city", "town", "municipality", "county", "village") if address.get(key)), None)
        try:
            item = location(city, address.get("state"), float(row.get("lat")), float(row.get("lon")), "geocoded")
        except (TypeError, ValueError):
            item = None
        if item:
            accepted.append(item)
    # Multiple results are acceptable only when they resolve to the same normalized city and province.
    signatures = {(normalize_text(item["city"]), normalize_text(item["province"])) for item in accepted}
    return accepted[0] if len(signatures) == 1 else None


# Second-level labels under .id that are registered per organization (e.g. unand.ac.id).
ID_SECOND_LEVEL = {"ac", "co", "or", "go", "sch", "web", "my", "net", "mil", "ponpes", "biz", "desa"}
# Shared hosting / journal platforms: a match on these says nothing about the publisher's location.
SHARED_DOMAINS = {"e-journal.id", "garuda.kemdikbud.go.id", "kemdikbud.go.id", "neliti.com", "ojs.id",
                  "journal.id", "academia.edu", "researchgate.net", "google.com", "blogspot.com",
                  "wordpress.com", "github.io", "doaj.org", "issn.org", "lipi.go.id", "brin.go.id"}
NAME_SYNONYMS = {"universitas": "", "university": "", "univ": "", "of": "", "the": "", "dan": "", "and": "",
                 "negeri": "state", "islam": "islamic", "teknologi": "technology", "institut": "institute",
                 "politeknik": "polytechnic", "kesehatan": "health", "sekolah": "school", "tinggi": "high",
                 "ilmu": "science", "sains": "science", "pendidikan": "education", "katolik": "catholic",
                 "kristen": "christian", "agama": "religion", "pertanian": "agriculture"}
ABBREVIATIONS = {"uin": "universitas islam negeri", "iain": "institut agama islam negeri",
                 "stain": "sekolah tinggi agama islam negeri", "unhas": "universitas hasanuddin"}
NOISE = re.compile(r"\b(press|penerbit|publishing|publisher|publications?|lppm|lp2m|lpm|"
                   r"lembaga penelitian dan pengabdian (kepada )?masyarakat)\b")
INSTITUTIONAL = re.compile(r"\b(universitas|university|institut|institute|politeknik|polytechnic|sekolah tinggi|"
                           r"akademi|academy|college|uin|iain|stain|stkip|stie|stikes)\b")


def registrable_domain(url):
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower().removeprefix("www.")
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 3 and parts[-1] == "id" and parts[-2] in ID_SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def name_key(name):
    """Order-free key that lets 'Universitas Jember' and 'University of Jember' meet."""
    text = normalize_text(name)
    text = " ".join(ABBREVIATIONS.get(token, token) for token in text.split())
    text = NOISE.sub(" ", text)
    tokens = {NAME_SYNONYMS.get(token, token) for token in text.split()}
    tokens.discard("")
    return " ".join(sorted(tokens)) if len(tokens) >= 1 and len("".join(tokens)) >= 4 else ""


def name_variants(name):
    """The full name plus each comma/dash separated segment (publisher strings often list unit, faculty, university)."""
    text = str(name or "")
    parts = [text] + [part for part in re.split(r"[,;/]|\s[-–]\s", text) if part.strip()]
    return [part.strip() for part in parts if part.strip()]


class InstitutionIndex:
    def __init__(self, institutions):
        self.by_domain, self.by_name, self.places = {}, {}, {}
        for row in institutions or []:
            place = from_openalex(row)
            if not place or not row.get("id"):
                continue
            self.places[row["id"]] = place
            domain = registrable_domain(row.get("homepage_url"))
            if domain and domain not in SHARED_DOMAINS:
                self.by_domain.setdefault(domain, set()).add(row["id"])
            key = name_key(row.get("name"))
            if key:
                self.by_name.setdefault(key, set()).add(row["id"])

    def _unique(self, ids, source):
        if len(ids or ()) != 1:
            return None
        institution_id = next(iter(ids))
        return dict(self.places[institution_id], source=source, institution_id=institution_id)

    def by_homepage(self, url):
        domain = registrable_domain(url)
        if not domain or domain in SHARED_DOMAINS:
            return None, "no_homepage" if not domain else "shared_host"
        ids = self.by_domain.get(domain)
        if not ids:
            return None, "unknown_domain"
        return (self._unique(ids, "homepage-domain"), None) if len(ids) == 1 else (None, "ambiguous_domain")

    def by_publisher_name(self, name, source="institution-name"):
        # Prefer the most specific segment that names exactly one institution.
        for variant in reversed(name_variants(name)):
            place = self._unique(self.by_name.get(name_key(variant)), source)
            if place:
                return place
        return None


def resolve(input_data, overrides, cache, client, max_new=None, cache_path=None, index=None):
    index = index or InstitutionIndex(input_data.get("institutions_index"))
    resolved, audit = {}, {"ambiguous_or_failed": [], "missing_host": [], "sources": {}}
    budget = {"new": 0, "max": max_new}
    for org_id, org in sorted(input_data.get("organizations", {}).items()):
        place = from_override(overrides.get(org_id))
        if not place:
            place = from_openalex(org)
        if not place and org.get("ror"):
            ror_key = "ror:" + str(org["ror"]).rstrip("/").rsplit("/", 1)[-1]
            if ror_key not in cache:
                cache[ror_key] = {"result": client.ror(org["ror"]), "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                if cache_path:
                    atomic_json(cache_path, cache)
            place = from_ror(cache[ror_key].get("result"))
        if not place:
            place = index.by_publisher_name(org.get("name"))
        if not place:
            place = geocode_name(org_id + ":" + normalize_text(org.get("name")), org.get("name"), cache, client, budget, cache_path)
        if place:
            resolved[org_id] = place
            audit["sources"][place["source"]] = audit["sources"].get(place["source"], 0) + 1
        else:
            audit["ambiguous_or_failed"].append({"id": org_id, "name": org.get("name")})
    known_ids = set(input_data.get("organizations", {}))
    audit["missing_host"] = [{"id": row["id"], "name": row["name"]} for row in input_data.get("journals", [])
                             if not row.get("publisher_id") or row.get("publisher_id") not in known_ids]
    audit["_budget"] = budget
    return resolved, audit, cache


def geocode_name(cache_key, name, cache, client, budget, cache_path, source="geocoded"):
    if not name:
        return None
    if cache_key not in cache and (budget["max"] is None or budget["new"] < budget["max"]):
        cache[cache_key] = {"query": f"{name}, Indonesia", "results": client.nominatim(name),
                            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        budget["new"] += 1
        if cache_path:
            atomic_json(cache_path, cache)
    place = from_nominatim((cache.get(cache_key) or {}).get("results"))
    return dict(place, source=source) if place else None


def registry_publishers(journal, registry):
    """Publisher names recorded for this journal's ISSNs in DOAJ (Indonesian records only) and Crossref."""
    from registry import normalize_issn  # local import keeps geocode usable without registry data
    names = []
    for value in list(journal.get("issn") or []) + [journal.get("issn_l")]:
        issn = normalize_issn(value)
        if not issn:
            continue
        doaj = ((registry.get("doaj") or {}).get("records") or {}).get(issn)
        if doaj and doaj.get("publisher") and str(doaj.get("country") or "Indonesia").lower() == "indonesia":
            names.append(doaj["publisher"])
        crossref = ((registry.get("crossref") or {}).get("records") or {}).get(issn)
        if crossref and crossref.get("publisher"):
            names.append(crossref["publisher"])
    return list(dict.fromkeys(names))


STREET = re.compile(r"^(jl|jln|jalan|komplek|kompleks|komp|gedung|gd|ruko|perum|perumahan|no|rt|rw|kav|blok|"
                    r"kampus|campus|po box|kotak pos|desa|dusun|kel|kelurahan|kec|kecamatan)\b|\d{3,}")


class KnownCities:
    """Cities already placed by OpenAlex/ROR/geocoding, so most addresses need no new geocoder call."""
    def __init__(self, places):
        self.by_pair, counts = {}, {}
        for place in places:
            city, province = normalize_text(normalize_city(place.get("city"))), normalize_text(place.get("province"))
            if not city or not province:
                continue  # without a province the place cannot vote or be returned
            self.by_pair.setdefault((city, province), place)
            counts.setdefault(city, {}).setdefault(province, 0)
            counts[city][province] += 1
        # A city named without a province goes to its usual province (at least 80% of the places seen).
        self.by_city = {}
        for city, provinces in counts.items():
            province, count = max(provinces.items(), key=lambda item: item[1])
            if count >= 0.8 * sum(provinces.values()):
                self.by_city[city] = self.by_pair[(city, province)]

    def find(self, city, province=None):
        city = normalize_text(normalize_city(re.sub(r"^(kota|kabupaten|kab)\.?\s+", "", str(city or ""), flags=re.I)))
        city = "jakarta" if re.match(r"^(dki )?jakarta( (pusat|barat|timur|selatan|utara))?$", city) else city
        if province and city != "jakarta":
            return self.by_pair.get((city, normalize_text(normalize_province(province))))
        return self.by_city.get(city)


PROVINCE_KEYS = {normalize_text(name) for name in PROVINCES}


def member_place(member, known, cache, client, budget, cache_path, gazetteer=None):
    """Crossref member address ("Padang, West Sumatra, Indonesia") -> city place, or None."""
    parts = [part.strip(" .") for part in str((member or {}).get("location") or "").split(",") if part.strip(" .")]
    parts = [part for part in parts if "http" not in part and "://" not in part]
    while parts and normalize_text(parts[-1]) in {"indonesia", "republic of indonesia", "ri"}:
        parts.pop()
    parts = [part for part in parts if not STREET.search(normalize_text(part))]
    if not parts:
        return None
    is_province = lambda part: normalize_text(normalize_province(part)) in PROVINCE_KEYS
    # "Jakarta" or "Jambi" alone is a city; a province only counts after the first part ("Padang, West Sumatra").
    province = next((part for part in parts[1:] if is_province(part)), None)
    cities = [part for part in parts if part is not province]
    if gazetteer:  # official kabupaten/kota, kecamatan and kelurahan names first
        for part in cities or parts:
            city, _ = gazetteer.locate(part, province or (parts[0] if len(parts) == 1 else None))
            if city:
                return location(city["city"], city["province"], city["latitude"], city["longitude"], "crossref-member")
    if len(parts) == 1 and is_province(parts[0]):
        place = known.find(parts[0], parts[0]) or known.find(parts[0])
        return dict(place, source="crossref-member") if place else None
    for part in cities:
        place = known.find(part, province)
        if place:
            return dict(place, source="crossref-member")
    query = ", ".join(cities + ([province] if province else []))
    place = geocode_name("member:" + normalize_text(query), query, cache, client, budget, cache_path, "crossref-member")
    if place and province and normalize_text(place.get("province")) != normalize_text(normalize_province(province)):
        return None  # the geocoder found a same-named place in another province
    return place


def crossref_member(journal, registry):
    from registry import normalize_issn
    member_of = (registry.get("crossref") or {}).get("member_of") or {}
    members = registry.get("crossref_members") or {}
    for value in [journal.get("issn_l")] + list(journal.get("issn") or []):
        member = members.get(member_of.get(normalize_issn(value) or ""))
        if member:
            return member
    return None


def resolve_journals(input_data, resolved, registry, cache, client, budget, cache_path=None, index=None):
    """Journal-level fallback for journals whose publisher is missing or could not be located."""
    index = index or InstitutionIndex(input_data.get("institutions_index"))
    known = KnownCities(list(index.places.values()) + list(resolved.values()))
    from wilayah import Gazetteer  # local import: wilayah itself imports this module
    gazetteer = Gazetteer.load()
    places, reasons, sources = {}, {}, {}
    for journal in input_data.get("journals", []):
        if journal.get("publisher_id") in resolved:
            continue
        place, reason = index.by_homepage(journal.get("homepage_url"))
        if not place:
            for name in registry_publishers(journal, registry):
                place = index.by_publisher_name(name, "registry-publisher")
                if not place and INSTITUTIONAL.search(normalize_text(name)):
                    place = geocode_name("pub:" + normalize_text(name), name, cache, client, budget, cache_path,
                                         "registry-publisher")
                if place:
                    break
            else:
                reason = reason if not journal.get("publisher_id") else "publisher_not_located"
        if not place:
            member = crossref_member(journal, registry)
            place = member_place(member, known, cache, client, budget, cache_path, gazetteer) if member else None
        if place:
            places[journal["id"]] = place
            sources[place["source"]] = sources.get(place["source"], 0) + 1
        else:
            reasons[reason or "not_located"] = reasons.get(reason or "not_located", 0) + 1
    return places, {"sources": sources, "unmapped_reasons": reasons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "build/input/openalex.json")
    parser.add_argument("--registry", type=Path, default=ROOT / "build/input/registry.json")
    parser.add_argument("--output", type=Path, default=ROOT / "build/input/locations.json")
    args = parser.parse_args()
    try:
        input_data = read_json(args.input)
        registry = read_json(args.registry) if args.registry.exists() else {}
        overrides = read_json(ROOT / "config/location-overrides.json")
        cache_path = ROOT / "data/geocode-cache.json"
        cache = read_json(cache_path)
        max_new_text = os.environ.get("MAX_NEW_GEOCODES", "").strip()
        max_new = int(max_new_text) if max_new_text else None
        client = PublicClient(os.environ.get("GEOCODER_CONTACT", "").strip())
        index = InstitutionIndex(input_data.get("institutions_index"))
        resolved, audit, cache = resolve(input_data, overrides, cache, client, max_new=max_new,
                                         cache_path=cache_path, index=index)
        budget = audit.pop("_budget")
        journal_places, journal_audit = resolve_journals(input_data, resolved, registry, cache, client, budget,
                                                         cache_path=cache_path, index=index)
        audit["journal_fallback"] = journal_audit
        result = {"schema_version": 2, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "locations": resolved, "journal_locations": journal_places, "audit": audit}
        atomic_json(cache_path, cache)
        atomic_json(args.output, result)
        print(json.dumps({"mapped_organizations": len(resolved), "unresolved_organizations": len(audit["ambiguous_or_failed"]),
                          "journal_fallback": journal_audit["sources"], "new_geocodes": budget["new"],
                          "output": str(args.output)}))
    except PipelineError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
