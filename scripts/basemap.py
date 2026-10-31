#!/usr/bin/env python3
"""Convert Natural Earth GeoJSON into the compact basemap used by the web map.

Run once when the basemap needs refreshing; the output is committed to web/assets/basemap.json.
Input files (public domain), from https://github.com/nvkelso/natural-earth-vector/tree/master/geojson:
  ne_10m_admin_0_countries.geojson, ne_10m_admin_1_states_provinces.geojson
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import ROOT, atomic_json

BOUNDS = (90.0, 146.0, -15.0, 10.0)  # lon_min, lon_max, lat_min, lat_max: slightly wider than the map view
TOLERANCE = 0.015                     # degrees, ~1.6 km; well below one screen pixel at the default zoom
MIN_SPAN = 0.03                       # drop islets smaller than this in both directions


def rings_of(geometry):
    if geometry["type"] == "Polygon":
        return geometry["coordinates"][:1]
    if geometry["type"] == "MultiPolygon":
        return [polygon[0] for polygon in geometry["coordinates"]]
    return []


def simplify(points, tolerance):
    """Iterative Douglas-Peucker."""
    if len(points) < 4:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        (x1, y1), (x2, y2) = points[start], points[end]
        dx, dy = x2 - x1, y2 - y1
        length = (dx * dx + dy * dy) ** 0.5
        best, index = 0.0, None
        for i in range(start + 1, end):
            x, y = points[i]
            distance = (abs(dy * x - dx * y + x2 * y1 - y2 * x1) / length if length
                        else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5)
            if distance > best:
                best, index = distance, i
        if index is not None and best > tolerance:
            keep[index] = True
            stack += [(start, index), (index, end)]
    return [point for point, kept in zip(points, keep) if kept]


def compact(ring):
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    if max(xs) < BOUNDS[0] or min(xs) > BOUNDS[1] or max(ys) < BOUNDS[2] or min(ys) > BOUNDS[3]:
        return None
    if max(xs) - min(xs) < MIN_SPAN and max(ys) - min(ys) < MIN_SPAN:
        return None
    points = simplify([(round(x, 3), round(y, 3)) for x, y in ring], TOLERANCE)
    points = [[round(x, 2), round(y, 2)] for x, y in points]
    deduped = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
    return deduped if len(deduped) >= 4 else None


def convert(countries_path, provinces_path):
    countries = json.loads(countries_path.read_text(encoding="utf-8"))
    provinces = json.loads(provinces_path.read_text(encoding="utf-8"))
    neighbors = []
    for feature in countries["features"]:
        if feature["properties"].get("ADM0_A3") == "IDN":
            continue
        neighbors += [ring for ring in map(compact, rings_of(feature["geometry"])) if ring]
    regions = []
    for feature in provinces["features"]:
        props = feature["properties"]
        if props.get("adm0_a3") != "IDN":
            continue
        rings = [ring for ring in map(compact, rings_of(feature["geometry"])) if ring]
        if rings:
            regions.append({"name": props.get("name"), "rings": rings})
    regions.sort(key=lambda row: row["name"] or "")
    return {"source": "Natural Earth 1:10m Admin 0 & Admin 1 (public domain), simplified",
            "provinces": regions, "neighbors": neighbors}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("countries", type=Path)
    parser.add_argument("provinces", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "web/assets/basemap.json")
    args = parser.parse_args()
    result = convert(args.countries, args.provinces)
    atomic_json(args.output, result)
    points = sum(len(r) for row in result["provinces"] for r in row["rings"]) + sum(len(r) for r in result["neighbors"])
    print(json.dumps({"provinces": len(result["provinces"]), "neighbor_rings": len(result["neighbors"]),
                      "points": points, "bytes": args.output.stat().st_size, "output": str(args.output)}))


if __name__ == "__main__":
    main()
