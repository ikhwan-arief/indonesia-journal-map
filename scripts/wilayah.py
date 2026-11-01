#!/usr/bin/env python3
"""Official Indonesian administrative areas (Kepmendagri kode wilayah) and a city lookup built on them.

`python3 scripts/wilayah.py` downloads the data from github.com/cahyadsn/wilayah (MIT; Kepmendagri
No 300.2.2-2138 Tahun 2025): 38 provinces, 514 regencies/cities (kabupaten/kota) with capital coordinates and
boundaries, 7,285 districts (kecamatan) and 83,762 villages (kelurahan/desa). It writes data/wilayah.json with
simplified boundaries.

`Gazetteer.locate()` turns a place name (a city, regency, district or village) and/or a coordinate into its
kabupaten/kota. A kota and a kabupaten with the same name in one province (Kota/Kabupaten Bogor) count as one
city, and the five administrative cities of Jakarta count as Jakarta.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import urllib.request
from pathlib import Path

from common import ROOT, PipelineError, atomic_json, read_json
from geocode import PROVINCES, normalize_province, normalize_text

SOURCE = "https://raw.githubusercontent.com/cahyadsn/wilayah/master/db/"
CITATION = ("Kode dan data wilayah administrasi pemerintahan, Kepmendagri No 300.2.2-2138 Tahun 2025, "
            "via github.com/cahyadsn/wilayah (MIT)")
DEFAULT_PATH = ROOT / "data/wilayah.json"
DIRECTIONS = {"south": "selatan", "north": "utara", "east": "timur", "west": "barat", "central": "tengah"}
PREFIX = re.compile(r"^(kota administrasi|kabupaten administrasi|kota|kabupaten|kab|kec|kecamatan|kelurahan|kel|desa|"
                    r"city of|regency of|district of)\s+")
SUFFIX = re.compile(r"\s+(city|regency|district|subdistrict|sub district|village|municipality)$")


# ---------- Building data/wilayah.json ----------

def simplify(ring, tolerance):
    """Douglas-Peucker on [lat, lng] points."""
    if len(ring) < 4:
        return ring
    keep, stack = {0, len(ring) - 1}, [(0, len(ring) - 1)]
    while stack:
        start, end = stack.pop()
        (ay, ax), (by, bx) = ring[start], ring[end]
        length = math.hypot(bx - ax, by - ay)
        best, index = 0.0, None
        for i in range(start + 1, end):
            y, x = ring[i]
            # A closed ring starts and ends on one point: measure from that point instead of a line.
            distance = (abs((bx - ax) * (ay - y) - (ax - x) * (by - ay)) / length if length > 1e-12
                        else math.hypot(x - ax, y - ay))
            if distance > best:
                best, index = distance, i
        if index is not None and best > tolerance:
            keep.add(index)
            stack += [(start, index), (index, end)]
    return [ring[i] for i in sorted(keep)]


def rings_of(path):
    """Boundary JSON nested to any depth -> list of rings (lists of [lat, lng] points)."""
    if path and isinstance(path[0], (int, float)):
        return []
    if path and all(isinstance(point, list) and len(point) == 2 and isinstance(point[0], (int, float)) for point in path):
        return [path]
    return [ring for part in path if isinstance(part, list) for ring in rings_of(part)]


def parse(codes_sql, boundaries_sql, tolerance=0.003):
    unescape = lambda value: value.replace("\\'", "'").replace("''", "'").strip()
    names = {code: unescape(name) for code, name in re.findall(r"\('([\d.]+)','((?:[^'\\]|\\.|'')*)'\)", codes_sql)}
    regions = {}
    # Boundaries never contain quotes, so a record runs from its code to the closing quote, across line breaks.
    record = re.compile(r"\('([\d.]+)','((?:[^'\\]|\\.)*)','((?:[^'\\]|\\.)*)',\s*(-?[\d.]+|NULL),\s*(-?[\d.]+|NULL),"
                        r"[^']*'(\[[^']*\])'")
    for code, name, capital, lat, lng, path in record.findall(boundaries_sql):
        path = re.sub(r"\s+", "", path)
        path += "]" * max(0, path.count("[") - path.count("]"))  # Kota Banjar's record lacks its final bracket
        boundary = json.loads(re.sub(r"\]\[", "],[", path))
        name, capital = unescape(name), unescape(capital)
        rings = [simplify([[round(y, 4), round(x, 4)] for y, x in ring], tolerance) for ring in rings_of(boundary)]
        regions[code] = {"name": name, "capital": capital.strip(), "lat": float(lat) if lat != "NULL" else None,
                         "lng": float(lng) if lng != "NULL" else None, "rings": [ring for ring in rings if len(ring) >= 3]}
    provinces = {code: name for code, name in names.items() if code.count(".") == 0}
    regencies = {code: {key: regions.get(code, {}).get(key) for key in ("capital", "lat", "lng", "rings")} | {"name": name}
                 for code, name in names.items() if code.count(".") == 1}
    missing = [code for code, row in regencies.items() if row["lat"] is None or not row["rings"]]
    if len(provinces) != 38 or len(regencies) < 500 or missing:
        raise PipelineError(f"Unexpected wilayah data: {len(provinces)} provinces, {len(regencies)} regencies, "
                            f"missing coordinates for {missing[:5]}")
    return {"schema_version": 1, "source": CITATION, "provinces": provinces, "regencies": regencies,
            "districts": {code: name for code, name in names.items() if code.count(".") == 2},
            "villages": {code: name for code, name in names.items() if code.count(".") == 3}}


def download(name):
    request = urllib.request.Request(SOURCE + name, headers={"User-Agent": "IndonesiaJournalMap/1.0"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read().decode("utf-8")


# ---------- Lookup ----------

def clean(name):
    """'Kota Administrasi Jakarta Selatan', 'South Tangerang City', 'Kec. Tembalang' -> comparable keys."""
    text = normalize_text(name)
    for _ in range(2):
        text = SUFFIX.sub("", PREFIX.sub("", text)).strip()
    first, _, rest = text.partition(" ")
    if first in DIRECTIONS and rest:
        text = f"{rest} {DIRECTIONS[first]}"
    return text


def city_name(regency_name):
    name = re.sub(r"^(Kota Administrasi|Kabupaten Administrasi|Kabupaten|Kota)\s+", "", regency_name.strip())
    return "Jakarta" if regency_name.startswith("Kota Administrasi Jakarta") else name


def inside(lat, lng, ring):
    hit, j = False, len(ring) - 1
    for i in range(len(ring)):
        (yi, xi), (yj, xj) = ring[i], ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            hit = not hit
        j = i
    return hit


class Gazetteer:
    def __init__(self, data):
        self.source = data.get("source")
        province_of = {code: normalize_province(name) for code, name in data["provinces"].items()}
        unknown = [name for name in province_of.values() if name not in PROVINCES]
        if unknown:
            raise PipelineError("Wilayah provinces without a canonical name: " + ", ".join(unknown))
        self.cities, self.group_of, self.areas = {}, {}, []
        for code, row in sorted(data["regencies"].items(), key=lambda item: (item[1]["name"].startswith("Kabupaten"), item[0])):
            province = province_of[code.split(".")[0]]
            key = (normalize_text(city_name(row["name"])), province)
            city = self.cities.setdefault(key, {"city": city_name(row["name"]), "province": province, "latitude": row["lat"],
                                                "longitude": row["lng"], "codes": []})  # kota first: its centre wins
            city["codes"].append(code)
            self.group_of[code] = key
            for ring in row["rings"]:
                lats, lngs = [p[0] for p in ring], [p[1] for p in ring]
                self.areas.append((min(lats), max(lats), min(lngs), max(lngs), ring, key))
        self.by_city, self.by_capital, self.by_district, self.by_village = {}, {}, {}, {}
        for code, row in data["regencies"].items():
            for alias in {clean(row["name"]), clean(city_name(row["name"]))}:
                self.by_city.setdefault(alias, set()).add(self.group_of[code])
            if row.get("capital"):  # e.g. Purwokerto is the seat of Kabupaten Banyumas
                self.by_capital.setdefault(clean(row["capital"]), set()).add(self.group_of[code])
        for table, index in ((data["districts"], self.by_district), (data["villages"], self.by_village)):
            for code, name in table.items():
                group = self.group_of.get(".".join(code.split(".")[:2]))
                if group:
                    index.setdefault(clean(name), set()).add(group)

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        return cls(read_json(path)) if Path(path).exists() else None

    def at(self, lat, lng):
        for south, north, west, east, ring, key in self.areas:
            if south <= lat <= north and west <= lng <= east and inside(lat, lng, ring):
                return key
        return None

    def _pick(self, groups, province, lat, lng, need_hint):
        if province:
            groups = {group for group in groups if group[1] == province}
        if len(groups) == 1 and (not need_hint or province or lat is not None):
            return next(iter(groups))
        if len(groups) > 1 and lat is not None:
            nearest = min(groups, key=lambda g: math.hypot(self.cities[g]["latitude"] - lat, self.cities[g]["longitude"] - lng))
            city = self.cities[nearest]
            if math.hypot(city["latitude"] - lat, city["longitude"] - lng) < 0.75:
                return nearest
        return None

    def _near(self, group, lat, lng, limit=0.75):
        city = self.cities[group]
        return math.hypot(city["latitude"] - lat, city["longitude"] - lng) < limit

    def locate(self, name=None, province=None, lat=None, lng=None):
        """-> (city record, method) or (None, None). Methods: city, capital, district, village, boundary.

        Order: kabupaten/kota name; seat of a kabupaten (Purwokerto -> Banyumas); district or village name in the
        given province or near the point (Tembalang -> Semarang); finally the boundary that contains the point."""
        province = normalize_province(province) if province else None
        province = province if province in PROVINCES else None
        key = clean(name) if name else ""
        has_point = lat is not None and lng is not None
        point = (lat, lng) if has_point else (None, None)
        if key:
            for index, method in ((self.by_city, "city"), (self.by_capital, "capital")):
                group = self._pick(index.get(key, set()), province, *point, need_hint=False)
                if group:
                    return self.cities[group], method
            # "Purwokerto" names no district, but Purwokerto Utara/Selatan/Barat/Timur all lie in one regency.
            prefixed = {group for name, groups in self.by_district.items() if name.startswith(key + " ") for group in groups}
            # District and village names repeat across the country: they need a province or a nearby point. A name
            # near the point beats the point itself, whose source may be a little off ("Ciputat" just outside
            # Tangerang Selatan).
            for groups, method in ((self.by_district.get(key, set()), "district"), (prefixed, "district"),
                                   (self.by_village.get(key, set()), "village")):
                group = self._pick(groups, province, *point, need_hint=True)
                if group and (not has_point or self._near(group, lat, lng)):
                    return self.cities[group], method
        if has_point:
            group = self.at(lat, lng)
            if group:
                return self.cities[group], "boundary"
        return None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--tolerance", type=float, default=0.003, help="boundary simplification in degrees")
    parser.add_argument("--source-dir", type=Path, help="read wilayah.sql and wilayah_level_1_2.sql from here")
    args = parser.parse_args()
    read = (lambda name: (args.source_dir / name).read_text(encoding="utf-8")) if args.source_dir else download
    try:
        data = parse(read("wilayah.sql"), read("wilayah_level_1_2.sql"), args.tolerance)
        Gazetteer(data)  # fails on province names that do not map to the canonical 38
        atomic_json(args.output, data)
        print(json.dumps({"provinces": len(data["provinces"]), "regencies": len(data["regencies"]),
                          "districts": len(data["districts"]), "villages": len(data["villages"]),
                          "output": str(args.output)}))
    except PipelineError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
