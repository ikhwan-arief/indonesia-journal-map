#!/usr/bin/env python3
"""Validate a built Indonesia journal map before it is published."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import PipelineError, read_json
from geocode import valid_coordinates

FORBIDDEN = [re.compile(rb"api[_-]?key\s*[=:]\s*[^\s&\"']+", re.I),
             re.compile(rb"authorization\s*[=:]\s*bearer\s+", re.I),
             re.compile(rb"[?&]api_key=", re.I)]


PUBLISHER_TYPES = {"education", "government", "private", "other", None}


def validate_snapshot(snapshot):
    errors = []
    if snapshot.get("country", {}).get("code") != "ID":
        errors.append("country.code must be ID")
    summary, journals, cities = snapshot.get("summary", {}), snapshot.get("journals", []), snapshot.get("cities", [])
    ids = [row.get("id") for row in journals]
    if not ids or any(not re.fullmatch(r"S\d+", str(value or "")) for value in ids):
        errors.append("journals must have OpenAlex Source IDs")
    if len(ids) != len(set(ids)):
        errors.append("journal IDs are not unique")
    if summary.get("total_journals") != len(journals):
        errors.append("total_journals does not match journals")
    mapped = sum(int(city.get("journal_count") or 0) for city in cities)
    unmapped = sum(row.get("location", {}).get("status") == "unmapped" for row in journals)
    if mapped != summary.get("mapped_journals") or unmapped != summary.get("unmapped_journals") or mapped + unmapped != len(journals):
        errors.append("mapped/unmapped totals do not reconcile")
    city_ids = {row.get("id") for row in cities}
    for city in cities:
        if not valid_coordinates(city.get("latitude"), city.get("longitude")):
            errors.append(f"city {city.get('id')} is outside configured Indonesia bounds")
    diamond = sum(row.get("diamond") is True for row in journals)
    if "diamond_journals" in summary and summary["diamond_journals"] != diamond:
        errors.append("diamond_journals does not match journals")
    doaj = sum(row.get("is_in_doaj") is True for row in journals)
    if "doaj_journals" in summary and summary["doaj_journals"] != doaj:
        errors.append("doaj_journals does not match journals")
    for journal in journals:
        if journal.get("diamond") is True and journal.get("is_in_doaj") is not True:
            errors.append(f"journal {journal.get('id')} is diamond but not marked as in DOAJ")
        if (journal.get("publisher") or {}).get("type") not in PUBLISHER_TYPES:
            errors.append(f"journal {journal.get('id')} has an invalid publisher type")
        if journal.get("diamond") not in (True, False, None):
            errors.append(f"journal {journal.get('id')} has an invalid diamond flag")
        for key in ("field", "domain"):
            value = journal.get(key)
            if value is not None and not (isinstance(value, dict) and value.get("id") and value.get("name")):
                errors.append(f"journal {journal.get('id')} has an invalid {key}")
        loc = journal.get("location", {})
        if loc.get("status") == "mapped" and loc.get("city_id") not in city_ids:
            errors.append(f"journal {journal.get('id')} references an unknown city")
    if errors:
        raise PipelineError("; ".join(errors[:20]))


def validate_site(site):
    required = [site / "index.html", site / "assets/app.js", site / "assets/style.css", site / "assets/basemap.json",
                site / "data/journals-map.json"]
    missing = [str(path.relative_to(site)) for path in required if not path.is_file()]
    if missing:
        raise PipelineError("Missing site files: " + ", ".join(missing))
    validate_snapshot(read_json(site / "data/journals-map.json"))
    for path in site.rglob("*"):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if any(pattern.search(payload) for pattern in FORBIDDEN):
            raise PipelineError(f"Possible credential or credential URL in {path.relative_to(site)}")
    app = (site / "assets/app.js").read_text(encoding="utf-8")
    if re.search(r"fetch\s*\(\s*['\"]https?://", app):
        raise PipelineError("The browser application must not fetch remote APIs.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", nargs="?", type=Path, default=Path("build/site"))
    args = parser.parse_args()
    try:
        validate_site(args.site)
        print(json.dumps({"valid": True, "site": str(args.site)}))
    except PipelineError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
