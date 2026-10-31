#!/usr/bin/env python3
"""Collect Indonesian journal Sources and their host organizations from OpenAlex."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import ROOT, PipelineError, atomic_json, read_json, require_api_key, short_id

SOURCE_FIELDS = ("id,display_name,issn,issn_l,is_oa,is_in_doaj,works_count,homepage_url,"
                 "host_organization,host_organization_name,host_organization_lineage,type,country_code,topics")
INSTITUTION_FIELDS = "id,display_name,homepage_url,ror,geo,type"
RETRY_CODES = {429, 500, 502, 503, 504}
ID_BATCH = 50


class Client:
    """Sequential API client whose credentials never enter URLs or exception text."""

    def __init__(self, key: str, timeout: int = 45, interval: float = 0.11):
        self.key, self.timeout, self.interval = key, timeout, interval
        self.last_call = 0.0
        self.calls = 0

    def get(self, path: str, params: dict | None = None):
        url = "https://api.openalex.org" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self.key,
            "User-Agent": "IndonesiaJournalMap/1.0",
            "Accept": "application/json",
        })
        for attempt in range(5):
            time.sleep(max(0, self.last_call + self.interval - time.monotonic()))
            self.last_call = time.monotonic()
            self.calls += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in RETRY_CODES or attempt == 4:
                    raise PipelineError(f"OpenAlex returned HTTP {error.code}; previous data retained.") from None
                retry = error.headers.get("Retry-After", "")
                time.sleep(min(30, float(retry)) if retry.isdigit() else min(16, 2 ** attempt))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                if attempt == 4:
                    raise PipelineError("OpenAlex request failed; previous data retained.") from None
                time.sleep(min(16, 2 ** attempt))
        raise PipelineError("OpenAlex request failed; previous data retained.")

    def pages(self, path: str, params: dict):
        cursor, seen_cursors, rows = "*", set(), {}
        expected = None
        while cursor:
            if cursor in seen_cursors:
                raise PipelineError("OpenAlex returned a repeated cursor.")
            seen_cursors.add(cursor)
            payload = self.get(path, dict(params, cursor=cursor, **{"per-page": 200}))
            if expected is None:
                expected = payload.get("meta", {}).get("count")
                if not isinstance(expected, int):
                    raise PipelineError("OpenAlex list response has no valid count.")
            results = payload.get("results")
            if not isinstance(results, list):
                raise PipelineError("OpenAlex list response has no results array.")
            for row in results:
                if row.get("id"):
                    rows[row["id"]] = row
            next_cursor = payload.get("meta", {}).get("next_cursor")
            if not results and next_cursor:
                raise PipelineError("OpenAlex returned an empty page with a continuation cursor.")
            cursor = next_cursor
        if expected and len(rows) < expected * 0.99:
            raise PipelineError(f"Incomplete collection: {len(rows)} of approximately {expected} sources.")
        return list(rows.values()), expected


def batches(values, size=ID_BATCH):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def primary_field(source):
    """The field (and its domain) carrying the most topic-tagged works for this source."""
    totals, meta = {}, {}
    for topic in source.get("topics") or []:
        field, domain = topic.get("field") or {}, topic.get("domain") or {}
        if not field.get("id"):
            continue
        key = short_id(field["id"])
        totals[key] = totals.get(key, 0) + int(topic.get("count") or 0)
        meta[key] = ({"id": key, "name": field.get("display_name")},
                     {"id": short_id(domain.get("id")), "name": domain.get("display_name")} if domain.get("id") else None)
    if not totals:
        return None, None
    best = max(sorted(totals), key=lambda key: totals[key])
    return meta[best]


def compact_source(source):
    field, domain = primary_field(source)
    return {
        "id": short_id(source.get("id")), "name": source.get("display_name") or short_id(source.get("id")),
        "issn": source.get("issn") or [], "issn_l": source.get("issn_l"),
        "is_oa": source.get("is_oa"), "is_in_doaj": source.get("is_in_doaj"),
        "works_count": source.get("works_count") or 0, "homepage_url": source.get("homepage_url"),
        "publisher_id": short_id(source.get("host_organization")),
        "publisher_name": source.get("host_organization_name"),
        "publisher_lineage": [short_id(value) for value in source.get("host_organization_lineage") or []],
        "field": field, "domain": domain,
    }


def compact_organization(row, kind):
    geo = row.get("geo") or {}
    return {"id": short_id(row.get("id")), "name": row.get("display_name"), "kind": kind, "type": row.get("type"),
            "ror": row.get("ror") or (row.get("ids") or {}).get("ror"), "country_codes": row.get("country_codes") or ([row.get("country_code")] if row.get("country_code") else []),
            "geo": {"city": geo.get("city"), "region": geo.get("region"), "country_code": geo.get("country_code"),
                    "latitude": geo.get("latitude"), "longitude": geo.get("longitude")}}


def collect(client: Client, country: dict):
    code = country["code"]
    identity = client.get("/countries/" + code)
    if short_id(identity.get("id")) != code or identity.get("display_name") != country["name"]:
        raise PipelineError("OpenAlex country identity does not match config/country.json.")
    sources, expected = client.pages("/sources", {"filter": f"country_code:{code},type:journal", "select": SOURCE_FIELDS})
    clean = [compact_source(row) for row in sources if row.get("type") == "journal" and row.get("country_code") == code]
    if len(clean) != len(sources):
        raise PipelineError("OpenAlex returned a source outside the requested journal/country filter.")
    organization_ids = sorted({row["publisher_id"] for row in clean if row["publisher_id"]})
    # host_organization can be a Publisher (P...) or an Institution (I...); each endpoint rejects the other type.
    publisher_ids = [value for value in organization_ids if value.startswith("P")]
    organizations, institution_for_publisher = {}, {}
    for group in batches(publisher_ids):
        payload = client.get("/publishers", {"filter": "ids.openalex:" + "|".join(group), "per-page": 100})
        for row in payload.get("results") or []:
            item = compact_organization(row, "publisher")
            organizations[item["id"]] = item
            for role in row.get("roles") or []:
                if role.get("role") == "institution" and role.get("id"):
                    institution_for_publisher[item["id"]] = short_id(role["id"])
    # Some Sources are hosted directly by an Institution ID rather than a Publisher ID.
    institution_ids = sorted({value for value in organization_ids if value.startswith("I")}
                             | set(institution_for_publisher.values()))
    institution_rows = {}
    for group in batches(institution_ids):
        payload = client.get("/institutions", {"filter": "ids.openalex:" + "|".join(group), "per-page": 100})
        for row in payload.get("results") or []:
            institution_rows[short_id(row.get("id"))] = compact_organization(row, "institution")
    for org_id in organization_ids:
        institution = institution_rows.get(institution_for_publisher.get(org_id)) or institution_rows.get(org_id)
        if institution:
            current = organizations.get(org_id, {"id": org_id, "name": institution.get("name"), "kind": "institution",
                                                  "ror": None, "country_codes": [], "geo": {}})
            current["geo"] = institution["geo"]
            current["ror"] = current.get("ror") or institution.get("ror")
            current["institution_id"] = institution["id"]
            current["institution_type"] = institution.get("type")
            organizations[org_id] = current
    institutions, _ = client.pages("/institutions", {"filter": f"country_code:{code}", "select": INSTITUTION_FIELDS})
    institutions_index = [dict(compact_organization(row, "institution"), homepage_url=row.get("homepage_url"))
                          for row in institutions]
    return {"schema_version": 1, "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "country": country, "expected_count": expected, "journals": clean,
            "organizations": organizations, "institutions_index": institutions_index, "api_calls": client.calls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/input/openalex.json")
    args = parser.parse_args()
    try:
        result = collect(Client(require_api_key()), read_json(ROOT / "config/country.json"))
        atomic_json(args.output, result)
        print(json.dumps({"journals": len(result["journals"]), "organizations": len(result["organizations"]),
                          "api_calls": result["api_calls"], "output": str(args.output)}))
    except PipelineError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
