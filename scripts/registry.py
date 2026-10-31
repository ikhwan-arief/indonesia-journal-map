#!/usr/bin/env python3
"""Fetch ISSN-keyed journal records from DOAJ (APC status, title, publisher) and Crossref (title, publisher).

Only ISSNs present in build/input/openalex.json are kept. DOAJ is reused from data/registry-cache.json for
REGISTRY_MAX_AGE_DAYS (default 30). Crossref is looked up per ISSN, at most CROSSREF_MAX_LOOKUPS (default 3000)
per run, prioritising odd titles and unmapped journals; each run continues where the last one stopped.

Crossref members (the organisations that register DOIs) carry the publisher's address. For journals without a
location, the member behind the journal's DOIs is looked up per ISSN (at most CROSSREF_MAX_MEMBER_LOOKUPS,
default 10000, per run) and the Indonesian member list is cached for REGISTRY_MAX_AGE_DAYS.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import http.client
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import ROOT, PipelineError, atomic_json, read_json

DOAJ_CSV = "https://doaj.org/csv"
CROSSREF_JOURNALS = "https://api.crossref.org/journals"
CROSSREF_MEMBERS = "https://api.crossref.org/members"
ISSN_PATTERN = re.compile(r"^(\d{4})-?(\d{3}[\dX])$")


def normalize_issn(value):
    match = ISSN_PATTERN.match(str(value or "").strip().upper())
    return f"{match.group(1)}-{match.group(2)}" if match else None


def wanted_issns(source):
    issns = set()
    for row in source.get("journals", []):
        for value in list(row.get("issn") or []) + [row.get("issn_l")]:
            issn = normalize_issn(value)
            if issn:
                issns.add(issn)
    return issns


def fetch(url, contact, timeout=120, attempts=6):
    agent = "IndonesiaJournalMap/1.0" + (f" (mailto:{contact})" if contact else "")
    request = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": "*/*"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise PipelineError(f"Registry returned HTTP {error.code}; previous data retained.") from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
            if attempt == attempts - 1:
                raise PipelineError("Registry request failed; previous data retained.") from None
        time.sleep(min(30, 2 ** (attempt + 1)))
    raise PipelineError("Registry request failed; previous data retained.")


def parse_doaj(text, issns):
    """DOAJ journal CSV -> {issn: record}. Diamond = no APC and no other fees."""
    rows = csv.DictReader(io.StringIO(text))
    columns = {name.strip().lower(): name for name in rows.fieldnames or []}
    needed = ["journal title", "journal issn (print version)", "journal eissn (online version)",
              "publisher", "country of publisher", "apc", "has other fees"]
    missing = [name for name in needed if name not in columns]
    if missing:
        raise PipelineError("DOAJ CSV is missing columns: " + ", ".join(missing))
    col = lambda row, name: (row.get(columns[name]) or "").strip()
    result = {}
    for row in rows:
        record = {"title": col(row, "journal title") or None, "publisher": col(row, "publisher") or None,
                  "country": col(row, "country of publisher") or None,
                  "has_apc": col(row, "apc").lower() == "yes",
                  "has_other_fees": col(row, "has other fees").lower() == "yes"}
        for name in ("journal issn (print version)", "journal eissn (online version)"):
            issn = normalize_issn(col(row, name))
            if issn and issn in issns:
                result[issn] = record
    return result


SUSPICIOUS_TITLE = re.compile(r'^["“\']|^(diterbitkan|published by|dikelola|managed by|p-?issn|e-?issn|issn|vol(ume)?\b|'
                              r'fakultas|universitas|program studi|jurusan|lembaga|faculty|department)', re.I)


def journal_issns(row):
    return [issn for issn in dict.fromkeys(normalize_issn(value) for value in [row.get("issn_l")] + list(row.get("issn") or [])) if issn]


def crossref_queue(source, doaj, located_publishers=()):
    """Journals ordered by how much a Crossref record helps: odd titles, then unmapped journals missing from DOAJ, then the rest."""
    def rank(row):
        if SUSPICIOUS_TITLE.search(row.get("name") or ""):
            return 0
        in_doaj = any(issn in doaj for issn in journal_issns(row))
        unmapped = row.get("publisher_id") not in located_publishers
        return 1 if unmapped and not in_doaj else 2
    rows = [row for row in source.get("journals", []) if journal_issns(row)]
    return sorted(rows, key=lambda row: (rank(row), row["id"]))


def crossref_lookup(issn, contact):
    params = urllib.parse.urlencode({"mailto": contact}) if contact else ""
    try:
        payload = json.loads(fetch(f"{CROSSREF_JOURNALS}/{issn}?{params}", contact, timeout=60, attempts=3))
    except PipelineError as error:
        return None if "HTTP 404" in str(error) else False  # False = transient failure, retry next run
    message = payload.get("message") or {}
    title = str(message.get("title") or "").strip()
    return {"title": title, "publisher": (message.get("publisher") or "").strip() or None} if title else None


def fetch_crossref(queue, cache, contact, budget, save, workers=3):
    """Per-ISSN lookups (the bulk listing is too slow at times). Resumable: results, including misses, are cached."""
    from concurrent.futures import ThreadPoolExecutor
    records, checked = cache.setdefault("records", {}), cache.setdefault("checked", {})
    todo = []
    for row in queue:
        issns = journal_issns(row)
        if any(issn in records for issn in issns) or all(issn in checked for issn in issns):
            continue
        todo.append(next(issn for issn in issns if issn not in checked))
        if len(todo) >= budget:
            break
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for issn, record in zip(todo, pool.map(lambda value: crossref_lookup(value, contact), todo)):
            if record is False:
                continue
            checked[issn] = dt.datetime.now(dt.timezone.utc).date().isoformat()
            if record:
                records[issn] = record
            done += 1
            if done % 200 == 0:
                save()
    save()
    return done, len(todo) < budget


def fetch_members(contact):
    """All Crossref members located in Indonesia -> {member_id: {name, location}}."""
    members, cursor = {}, "*"
    while cursor:
        params = {"rows": 1000, "cursor": cursor}  # the members route does not support select
        if contact:
            params["mailto"] = contact
        message = json.loads(fetch(f"{CROSSREF_MEMBERS}?{urllib.parse.urlencode(params)}", contact, timeout=180)).get("message") or {}
        items = message.get("items") or []
        for item in items:
            location = str(item.get("location") or "").strip()
            if location.lower().rstrip(". ").endswith("indonesia"):
                members[str(item["id"])] = {"name": item.get("primary-name"), "location": location}
        cursor = message.get("next-cursor") if items else None
    return members


def member_lookup(issn, contact):
    """Crossref member that registered DOIs for this ISSN; None if none, False on a transient failure."""
    params = {"rows": 1, "select": "member"}
    if contact:
        params["mailto"] = contact
    try:
        payload = json.loads(fetch(f"{CROSSREF_JOURNALS}/{issn}/works?{urllib.parse.urlencode(params)}", contact,
                                   timeout=60, attempts=3))
    except PipelineError as error:
        return None if "HTTP 404" in str(error) else False
    items = (payload.get("message") or {}).get("items") or []
    return str(items[0]["member"]) if items and items[0].get("member") else None


def fetch_member_ids(rows, cache, contact, budget, save, workers=4):
    """ISSN -> member id for the given journals. Resumable: answers, including misses, are cached."""
    from concurrent.futures import ThreadPoolExecutor
    member_of = cache.setdefault("member_of", {})
    todo = []
    for row in rows:
        issns = journal_issns(row)
        if any(member_of.get(issn) for issn in issns) or all(issn in member_of for issn in issns):
            continue
        todo.append(next(issn for issn in issns if issn not in member_of))
        if len(todo) >= budget:
            break
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for issn, member in zip(todo, pool.map(lambda value: member_lookup(value, contact), todo)):
            if member is False:
                continue
            member_of[issn] = member
            done += 1
            if done % 250 == 0:
                save()
    save()
    return done, len(todo) < budget


def unlocated_journals(source, locations_path):
    """Journals that neither their publisher nor a journal-level fallback has placed yet."""
    data = read_json(locations_path) if locations_path.exists() else {}
    located, placed = set(data.get("locations") or {}), set(data.get("journal_locations") or {})
    return [row for row in source.get("journals", []) if journal_issns(row)
            and row.get("publisher_id") not in located and row["id"] not in placed]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "build/input/openalex.json")
    parser.add_argument("--locations", type=Path, default=ROOT / "build/input/locations.json")
    parser.add_argument("--output", type=Path, default=ROOT / "build/input/registry.json")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/registry-cache.json")
    parser.add_argument("--refresh", action="store_true", help="download DOAJ again and recheck Crossref misses")
    args = parser.parse_args()
    try:
        source = read_json(args.input)
        issns = wanted_issns(source)
        contact = os.environ.get("GEOCODER_CONTACT", "").strip()
        max_age = dt.timedelta(days=int(os.environ.get("REGISTRY_MAX_AGE_DAYS", "30") or 30))
        budget = int(os.environ.get("CROSSREF_MAX_LOOKUPS", "3000") or 3000)
        cache = read_json(args.cache) if args.cache.exists() else {}
        now = dt.datetime.now(dt.timezone.utc)
        save = lambda: atomic_json(args.cache, cache)

        stamp = (cache.get("doaj") or {}).get("fetched_at")
        if args.refresh or not stamp or now - dt.datetime.fromisoformat(stamp) >= max_age:
            text = fetch(DOAJ_CSV, contact, timeout=300).decode("utf-8-sig")
            cache["doaj"] = {"fetched_at": now.isoformat(), "records": parse_doaj(text, issns)}
            save()
        crossref = cache.get("crossref") or {}
        if args.refresh or "checked" not in crossref:
            crossref = {"records": crossref.get("records", {}), "checked": {},
                        "member_of": {k: v for k, v in crossref.get("member_of", {}).items() if v}}
        cache["crossref"] = crossref
        located = set((read_json(args.locations).get("locations") or {}) if args.locations.exists() else {})
        queue = crossref_queue(source, cache["doaj"]["records"], located)
        done, complete = fetch_crossref(queue, crossref, contact, budget, save)
        crossref["fetched_at"] = now.isoformat()
        save()

        members = cache.get("crossref_members") or {}
        stamp = members.get("fetched_at")
        if args.refresh or not stamp or now - dt.datetime.fromisoformat(stamp) >= max_age:
            cache["crossref_members"] = members = {"fetched_at": now.isoformat(), "members": fetch_members(contact)}
            save()
        member_budget = int(os.environ.get("CROSSREF_MAX_MEMBER_LOOKUPS", "10000") or 10000)
        member_done, member_complete = fetch_member_ids(unlocated_journals(source, args.locations), crossref, contact,
                                                        member_budget, save)
        result = {"schema_version": 2, "doaj": cache["doaj"],
                  "crossref": {"records": crossref["records"],
                               "member_of": {k: v for k, v in crossref.get("member_of", {}).items() if v}},
                  "crossref_members": members["members"]}
        atomic_json(args.output, result)
        print(json.dumps({"issns": len(issns), "doaj": len(cache["doaj"]["records"]),
                          "crossref": len(crossref["records"]), "crossref_lookups": done,
                          "crossref_complete": complete, "indonesian_members": len(members["members"]),
                          "member_lookups": member_done, "member_lookups_complete": member_complete,
                          "output": str(args.output)}))
    except PipelineError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
