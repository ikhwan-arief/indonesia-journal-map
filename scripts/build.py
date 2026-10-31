#!/usr/bin/env python3
"""Build the public static snapshot and copy the dependency-free web application."""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from common import ROOT, PipelineError, atomic_json, read_json
from geocode import normalize_city, normalize_text, registrable_domain, valid_coordinates
from registry import SUSPICIOUS_TITLE, normalize_issn

PUBLISHER_STATEMENT = re.compile(r"^(diterbitkan|published by|dikelola|managed by)\b", re.I)
TITLE_SIMILARITY = 0.6


def city_id(place):
    return "-".join(filter(None, [normalize_text(normalize_city(place.get("city"))).replace(" ", "-"),
                                  normalize_text(place.get("province")).replace(" ", "-")]))


# Cities whose province changed after a split; some sources still carry the old province (Riau -> Kepulauan Riau, 2002).
PROVINCE_FIXES = {"batam": "Kepulauan Riau", "tanjung pinang": "Kepulauan Riau", "tanjungpinang": "Kepulauan Riau"}


def fill_missing_provinces(locations):
    """Give a province-less place the province of the nearest same-named city (within ~0.5°), and apply known fixes."""
    known = defaultdict(list)
    for place in locations.values():
        if place and place.get("city") and place.get("province"):
            known[normalize_text(normalize_city(place["city"]))].append(place)
    filled = {}
    for key, place in locations.items():
        if place and place.get("city"):
            name = normalize_text(normalize_city(place["city"]))
            if name in PROVINCE_FIXES and place.get("province") != PROVINCE_FIXES[name]:
                place = dict(place, province=PROVINCE_FIXES[name])
            elif not place.get("province") and known.get(name):
                distance = lambda other: abs(other["latitude"] - place["latitude"]) + abs(other["longitude"] - place["longitude"])
                nearest = min(known[name], key=distance)
                if distance(nearest) <= 0.5 or len({row["province"] for row in known[name]}) == 1:
                    place = dict(place, province=nearest["province"])
        filled[key] = place
    return filled


def journal_issns(row):
    return list(dict.fromkeys(filter(None, (normalize_issn(value) for value in list(row.get("issn") or []) + [row.get("issn_l")]))))


def registry_records(row, registry, name):
    records = (registry.get(name) or {}).get("records") or {}
    return [records[issn] for issn in journal_issns(row) if issn in records]


def corrected_title(row, registry):
    """Return (title, source). Replace the OpenAlex name only when the ISSN registry title clearly disagrees."""
    original = str(row.get("name") or "").strip()
    for name in ("doaj", "crossref"):
        titles = {record["title"].strip() for record in registry_records(row, registry, name) if record.get("title")}
        if not titles:
            continue
        if len({normalize_text(title) for title in titles}) != 1:
            return original, "openalex"  # ISSNs disagree; keep OpenAlex rather than guess
        candidate = sorted(titles)[0]
        a, b = normalize_text(original), normalize_text(candidate)
        if a == b:
            return original, "openalex"
        statement = (bool(PUBLISHER_STATEMENT.search(original.strip('"\'“” '))) or original[:1] in "\"“'"
                     or bool(SUSPICIOUS_TITLE.search(original)))
        similar = a == b or (a and b and (a in b or b in a)) or difflib.SequenceMatcher(None, a, b).ratio() >= TITLE_SIMILARITY
        # DOAJ titles are curated, so a clear mismatch is enough. Crossref titles are publisher-entered and often
        # stale, so they only replace names that are evidently not a journal title.
        if statement or (name == "doaj" and not similar):
            return candidate, name
        if name == "doaj":
            return original, "openalex"
    return original, "openalex"


def diamond_status(row, registry):
    records = registry_records(row, registry, "doaj")
    if not records:
        return None
    return any(not record.get("has_apc") and not record.get("has_other_fees") for record in records)


def registry_publisher(row, registry):
    for name in ("doaj", "crossref"):
        for record in registry_records(row, registry, name):
            if record.get("publisher"):
                return record["publisher"], name
    return None, None


# Publisher type: education | government | private | other. The order of the rules is in publisher_type().
# Domain extensions. Official Indonesian second-level domains are only issued to eligible bodies, so they are
# the first test; commercial extensions are open to anyone and count after the name rules.
EDUCATION_SUFFIXES = ("ac.id", "sch.id", "ponpes.id", "edu")
GOVERNMENT_SUFFIXES = ("go.id", "mil.id", "desa.id")
COMMERCIAL_SUFFIXES = ("com", "co.id", "biz.id", "biz")
OTHER_DOMAINS = {"or.id": "private"}
# Free hosting and portals say nothing about who publishes the journal.
PLATFORM_DOMAINS = {"blogspot.com", "wordpress.com", "academia.edu", "github.io", "google.com", "researchgate.net",
                    "neliti.com", "doaj.org", "issn.org", "weebly.com", "wixsite.com"}
INSTITUTION_TYPES = {"education": "education", "government": "government", "funder": "government",
                     "company": "private", "nonprofit": "private"}
# OpenAlex labels several state research bodies (LIPI, BATAN, Bank Indonesia) "facility" or "other", so these
# only apply when neither the domain nor the name says more.
VAGUE_INSTITUTION_TYPES = {"healthcare": "other", "facility": "other", "archive": "other", "other": "other"}
NAME_TYPES = [
    ("education", re.compile(r"\b(universitas|university|universiti|politeknik|polytechnic|sekolah tinggi|stai[a-z]*|stie|stikes|"
                             r"stkip|stit|stih|stmik|sttp?|uin|iain|stain|iaid|akademi|academy|college|fakultas|faculty|state islamic|"
                             r"pascasarjana|graduate school|program studi|study program|lppm|lp2m|lp3m|kampus|"
                             r"sekolah|school|madrasah|madrasah aliyah|sma|smk|smp|pesantren|poltekkes|poltek|amik|stisip|stks|sttb|stiq|"
                             r"stiba|institutum|seminari)\b")),
    # Strong private markers come before government words, e.g. a student association's "Badan".
    ("private", re.compile(r"\b(cv|pt|yayasan|foundation|perkumpulan|asosiasi|assosiasi|association|ikatan|society|himpunan|"
                           r"perhimpunan|persatuan|konsorsium|consortium|publisher|publishers|publishing|penerbit)\b")),
    ("government", re.compile(r"\b(kementerian|kementrian|ministry|badan|agency|brin|lipi|batan|bapeten|lapan|bmkg|bappeda|"
                              r"bappenas|balitbang[a-z]*|balai|pusat penelitian|pusat riset|pemerintah|pemkab|pemkot|"
                              r"pemprov|government|dinas|kepolisian|polri|mahkamah|komisi|bank indonesia|"
                              r"perpustakaan nasional|arsip nasional|indonesian institute of sciences|kejaksaan|"
                              r"direktorat|kantor wilayah|national nuclear|nuclear energy)\b")),
    ("education", re.compile(r"\b(institut|institute of technology|islamic institute|institut agama|teacher training|"
                             r"u[a-z]{1,4} press|uns)\b")),
]
# Hospitals (Rumah Sakit, RSUP, RSUD) are counted as government agencies, whatever their domain.
HOSPITAL = re.compile(r"\b(rumah sakit|rsup|rsud|rsu|rs|hospital)\b")
# ...but not colleges or associations named after a hospital ("STIKES RS Baptis", "Akademi Administrasi Rumah Sakit").
NOT_HOSPITAL = re.compile(r"\b(stikes|sekolah tinggi|akademi|institut|politeknik|asosiasi|assosiasi|association|ikatan)\b")
# Bodies counted as government agencies whatever their domain, by request (e.g. Majelis Ulama Indonesia).
GOVERNMENT_BODIES = re.compile(r"\b(majelis ulama indonesia|mui|lldikti|lembaga layanan pendidikan tinggi|"
                               r"lembaga layanan perguruan tinggi|kopertis|kopertais)\b")
# Foundations and associations are private even when OpenAlex types them otherwise ("Yayasan ..." as government).
ASSOCIATION = re.compile(r"\b(yayasan|foundation|perkumpulan|asosiasi|assosiasi|association|ikatan|himpunan|perhimpunan|"
                         r"persatuan|konsorsium|consortium)\b")
UNIVERSITY = re.compile(r"\b(university|universitas|universiti|akademi)\b")
# Companies (PT = Perseroan Terbatas, CV = Commanditaire Vennootschap) are private, whatever their domain.
COMPANY = re.compile(r"\b(pt|cv|perseroan terbatas|commanditaire vennootschap|persekutuan komanditer)\b")
# Generic words that suggest an independent publisher but are also used by university units ("UIR Press").
WEAK_PRIVATE = re.compile(r"\b(press|media|pustaka|lembaga|institute|komunitas|community|organization|organisasi|group|"
                          r"center|centre|pusat|research|forum|publication|publications)\b")


def domain_suffix(url):
    domain = registrable_domain(url)
    return ".".join(domain.split(".")[-2:]) if domain.endswith(".id") else ""


def domain_type(url):
    """-> education | government | commercial | None, from the domain extension of a website."""
    domain = registrable_domain(url)
    if not domain or domain in PLATFORM_DOMAINS:
        return None
    if domain.endswith(tuple("." + value for value in EDUCATION_SUFFIXES)):
        return "education"
    if domain.endswith(tuple("." + value for value in GOVERNMENT_SUFFIXES)):
        return "government"
    if domain.endswith(tuple("." + value for value in COMMERCIAL_SUFFIXES)):
        return "commercial"
    return None


def type_from_name(name):
    text = normalize_text(name)
    if not text:
        return None
    for kind, pattern in NAME_TYPES:
        if pattern.search(text):
            return kind
    return None


def manual_type(row, names, overrides):
    overrides = overrides or {}
    if row.get("publisher_id") in (overrides.get("ids") or {}):
        return overrides["ids"][row["publisher_id"]]
    for key, kind in (overrides.get("names") or {}).items():
        needle = f" {normalize_text(key)} "
        if needle.strip() and any(needle in f" {normalize_text(name)} " for name in names if name):
            return kind
    return None


def publisher_type(row, org, place, institutions, registry_names, overrides=None):
    """-> (education | government | private | other | None, source). institutions: id -> {type, homepage_url, name}.

    Order: manual override; official domain of the journal's site; PT/CV; hospitals, MUI, LLDIKTI/Kopertis;
    official domain of the publisher's institution; University/Akademi names; foundations and associations;
    OpenAlex institution type; commercial domains; other name keywords; weak signals."""
    names = [row.get("publisher_name") or org.get("name")] + list(registry_names)
    linked = [institutions.get(value) or {} for value in
              (org.get("institution_id"), row.get("publisher_id"), (place or {}).get("institution_id")) if value]
    kind = manual_type(row, names, overrides)
    if kind:
        return kind, "manual"
    sites = [row.get("homepage_url")] + [item.get("homepage_url") for item in linked]
    if domain_type(sites[0]) in ("education", "government"):  # the journal's own site
        return domain_type(sites[0]), "domain"
    # Name rules read the OpenAlex publisher name; DOAJ/Crossref names only stand in when OpenAlex has none.
    texts = [normalize_text(name) for name in (names[:1] if names[0] else names[1:]) if name]
    if any(COMPANY.search(text) for text in texts):
        return "private", "name"
    if any(HOSPITAL.search(text) and not NOT_HOSPITAL.search(text) for text in texts):
        return "government", "name"
    if any(GOVERNMENT_BODIES.search(text) for text in texts):
        return "government", "name"
    for url in sites[1:]:  # the site of the publisher's OpenAlex institution
        if domain_type(url) in ("education", "government"):
            return domain_type(url), "domain"
    if any(UNIVERSITY.search(text) for text in texts + [normalize_text(item.get("name")) for item in linked]):
        return "education", "name"
    if any(ASSOCIATION.search(text) for text in texts):
        return "private", "name"
    for item in linked:
        if item.get("type") in INSTITUTION_TYPES:
            return INSTITUTION_TYPES[item["type"]], "openalex"
    if any(domain_type(url) == "commercial" for url in sites):
        return "private", "domain"
    for name in names:
        kind = type_from_name(name)
        if kind:
            return kind, "name"
    for item in linked:
        if item.get("type") in VAGUE_INSTITUTION_TYPES:
            return VAGUE_INSTITUTION_TYPES[item["type"]], "openalex"
    if domain_suffix(row.get("homepage_url")) in OTHER_DOMAINS:
        return OTHER_DOMAINS[domain_suffix(row.get("homepage_url"))], "domain"
    if any(WEAK_PRIVATE.search(text) for text in texts):
        return "private", "name"
    return None, None


def registry_publishers(row, registry):
    return [record["publisher"] for name in ("doaj", "crossref") for record in registry_records(row, registry, name)
            if record.get("publisher")]


def build_snapshot(source, location_data, registry=None, type_overrides=None):
    registry = registry or {}
    merged = dict(location_data.get("locations", {}))
    merged.update({"journal:" + key: value for key, value in (location_data.get("journal_locations") or {}).items()})
    merged = fill_missing_provinces(merged)
    locations = {key: value for key, value in merged.items() if not key.startswith("journal:")}
    journal_locations = {key[8:]: value for key, value in merged.items() if key.startswith("journal:")}
    title_changes = []
    organizations = source.get("organizations", {})
    institutions = {row["id"]: {"type": row.get("type"), "homepage_url": row.get("homepage_url"), "name": row.get("name")}
                    for row in source.get("institutions_index") or [] if row.get("id")}
    for org in organizations.values():
        if org.get("institution_id") and org.get("institution_type"):
            entry = institutions.setdefault(org["institution_id"], {})
            entry["type"] = entry.get("type") or org["institution_type"]
    journals, city_journals, city_publishers, city_places, location_sources = [], defaultdict(int), defaultdict(set), {}, defaultdict(Counter)
    publisher_ids = set()
    seen = set()
    for row in source.get("journals", []):
        journal_id = row.get("id")
        if not journal_id or journal_id in seen:
            raise PipelineError("Journal IDs must be present and unique.")
        seen.add(journal_id)
        publisher_id = row.get("publisher_id") or ""
        if publisher_id:
            publisher_ids.add(publisher_id)
        org = organizations.get(publisher_id, {})
        place = locations.get(publisher_id) or journal_locations.get(journal_id)
        mapped = bool(place and valid_coordinates(place.get("latitude"), place.get("longitude")) and place.get("city"))
        cid = city_id(place) if mapped else None
        status, source_name = ("mapped", place.get("source")) if mapped else ("unmapped", None)
        if mapped:
            city_journals[cid] += 1
            city_publishers[cid].add(publisher_id or "unknown")
            city_places.setdefault(cid, place)
            location_sources[cid][source_name] += 1
        name, name_source = corrected_title(row, registry)
        if name_source != "openalex":
            title_changes.append({"id": journal_id, "openalex_name": row.get("name"), "name": name, "source": name_source,
                                  "issn": journal_issns(row)})
        publisher_name, publisher_source = row.get("publisher_name") or org.get("name"), "openalex"
        if not publisher_name:
            publisher_name, publisher_source = registry_publisher(row, registry)
        kind, kind_source = publisher_type(row, org, place, institutions, registry_publishers(row, registry), type_overrides)
        diamond = diamond_status(row, registry)
        journal = {"id": journal_id, "name": name or journal_id, "name_source": name_source,
                   "issn_l": row.get("issn_l"), "issn": row.get("issn") or [],
                   "is_oa": row.get("is_oa"),
                   # In DOAJ per OpenAlex, or matched by ISSN in the DOAJ journal list.
                   "is_in_doaj": bool(row.get("is_in_doaj")) or diamond is not None,
                   "diamond": diamond,
                   "works_count": int(row.get("works_count") or 0),
                   "field": row.get("field"), "domain": row.get("domain"),
                   "publisher": {"id": publisher_id or None, "name": publisher_name, "source": publisher_source,
                                 "type": kind, "type_source": kind_source},
                   "location": {"city_id": cid, "status": status, "source": source_name}}
        if name_source != "openalex":
            journal["openalex_name"] = row.get("name")
        journals.append(journal)
    cities = [{"id": cid, "name": normalize_city(place["city"]), "province": place.get("province"),
               "latitude": place["latitude"], "longitude": place["longitude"],
               "journal_count": city_journals[cid], "publisher_count": len(city_publishers[cid]),
               "location_sources": dict(location_sources[cid])}
              for cid, place in city_places.items()]
    cities.sort(key=lambda row: (-row["journal_count"], row["name"]))
    journals.sort(key=lambda row: row["name"].casefold())
    mapped = sum(row["journal_count"] for row in cities)
    total = len(journals)
    if mapped + sum(1 for row in journals if row["location"]["status"] == "unmapped") != total:
        raise PipelineError("Mapped and unmapped journal totals do not reconcile.")
    return {"schema_version": 2, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "country": source["country"],
            "summary": {"total_journals": total, "mapped_journals": mapped, "unmapped_journals": total - mapped,
                        "mapped_percent": round(100 * mapped / total, 1) if total else 0,
                        "city_count": len(cities), "publisher_count": len(publisher_ids),
                        "diamond_journals": sum(1 for row in journals if row["diamond"] is True),
                        "diamond_mapped": sum(1 for row in journals if row["diamond"] is True and row["location"]["status"] == "mapped"),
                        "doaj_matched": sum(1 for row in journals if row["diamond"] is not None),
                        "doaj_journals": sum(1 for row in journals if row["is_in_doaj"]),
                        "doaj_mapped": sum(1 for row in journals if row["is_in_doaj"] and row["location"]["status"] == "mapped"),
                        "publisher_types": dict(Counter(row["publisher"]["type"] or "unknown" for row in journals)),
                        "titles_corrected": len(title_changes)},
            "cities": cities, "journals": journals, "_title_changes": title_changes}


TYPE_OVERRIDES = ROOT / "config/publisher-type-overrides.json"
PUBLISHER_TYPES = {"education", "government", "private", "other"}


def build_site(input_path, locations_path, output, registry_path=None, type_overrides_path=TYPE_OVERRIDES):
    registry = read_json(registry_path) if registry_path and registry_path.exists() else {}
    overrides = read_json(type_overrides_path) if type_overrides_path and type_overrides_path.exists() else {}
    bad = {kind for kind in list((overrides.get("names") or {}).values()) + list((overrides.get("ids") or {}).values())
           if kind not in PUBLISHER_TYPES}
    if bad:
        raise PipelineError("Unknown publisher type in overrides: " + ", ".join(sorted(map(str, bad))))
    snapshot = build_snapshot(read_json(input_path), read_json(locations_path), registry, overrides)
    title_changes = snapshot.pop("_title_changes")
    previous = output / "data/journals-map.json"
    if previous.exists():
        old_total = read_json(previous).get("summary", {}).get("total_journals", 0)
        if old_total and snapshot["summary"]["total_journals"] < old_total * 0.9:
            raise PipelineError("Journal count dropped by more than 10%; previous site retained.")
    temporary = Path(tempfile.mkdtemp(prefix="indonesia-journal-map-", dir=output.parent if output.parent.exists() else ROOT / "build"))
    backup = output.with_name(output.name + ".previous")
    moved_previous = False
    try:
        temporary.chmod(0o755)  # mkdtemp creates 0700; web servers need to traverse the site directory.
        shutil.copytree(ROOT / "web", temporary, dirs_exist_ok=True)
        atomic_json(temporary / "data/journals-map.json", snapshot)
        audit = read_json(locations_path).get("audit", {})
        atomic_json(temporary / "data/location-audit.json", audit)
        atomic_json(temporary / "data/title-audit.json", {"rule": "DOAJ, then Crossref title by ISSN replaces the OpenAlex "
                    "name when it looks like a publisher statement or similarity < 0.6", "changes": title_changes})
        if backup.exists():
            shutil.rmtree(backup)
        if output.exists():
            output.rename(backup)
            moved_previous = True
        temporary.rename(output)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if moved_previous and backup.exists() and not output.exists():
            backup.rename(output)
        raise
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "build/input/openalex.json")
    parser.add_argument("--locations", type=Path, default=ROOT / "build/input/locations.json")
    parser.add_argument("--registry", type=Path, default=ROOT / "build/input/registry.json")
    parser.add_argument("--output", type=Path, default=ROOT / "build/site")
    args = parser.parse_args()
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        snapshot = build_site(args.input, args.locations, args.output, args.registry)
        print(json.dumps({"journals": snapshot["summary"]["total_journals"], "cities": snapshot["summary"]["city_count"],
                          "mapped_percent": snapshot["summary"]["mapped_percent"], "output": str(args.output)}))
    except PipelineError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
