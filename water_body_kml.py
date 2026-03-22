#!/usr/bin/env python3
"""
water_body_kml.py — Generate high-definition KML files for Canadian rivers and lakes.

Queries OpenStreetMap via the Overpass API and writes a styled KML file whose
LineString / Polygon coordinates come directly from every OSM node — no
geometry simplification is applied.  The result can be dropped straight into
a Google Maps overlay.

Usage
-----
    python water_body_kml.py "Peace River"
    python water_body_kml.py "Lake Athabasca"
    python water_body_kml.py "Fraser River" --output fraser.kml
    python water_body_kml.py "Lake Ontario" --type lake
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Optional
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

# ── constants ──────────────────────────────────────────────────────────────────

# Loose bounding box that covers all of Canada (south, west, north, east).
CANADA_BBOX = "41.7,-141.0,83.3,-52.6"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT = 180  # seconds for the server-side query

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "WaterBodyKML/1.0 (educational; Canadian rivers/lakes)"

# Retry settings for transient network / server errors
MAX_RETRIES = 4
RETRY_DELAYS = [2, 4, 8, 16]

# OSM class/type combinations that indicate a water body
_WATER_OSM_CLASSES = {
    ("waterway", "river"), ("waterway", "stream"), ("waterway", "canal"),
    ("natural", "water"), ("natural", "bay"), ("natural", "strait"),
    ("place", "sea"), ("place", "ocean"), ("place", "bay"),
}

# KML colours in AABBGGRR format (Google Earth / Maps convention).
# R=0xE9, G=0x9B, B=0x3D  →  a medium river-blue
RIVER_LINE_COLOR  = "ffE99B3D"
RIVER_LINE_WIDTH  = "3"
LAKE_FILL_COLOR   = "60E99B3D"   # 37 % opaque fill
LAKE_BORDER_COLOR = "ffE99B3D"
LAKE_BORDER_WIDTH = "2"


# ── Overpass helpers ───────────────────────────────────────────────────────────

def _build_query(name: str, feature_type: str) -> str:
    """Return an Overpass QL query string for *name* restricted to Canada."""
    escaped = name.replace('"', '\\"')
    bbox = CANADA_BBOX

    if feature_type == "river":
        tags = [
            '["waterway"="river"]',
            '["waterway"="stream"]',
            '["waterway"="canal"]',
        ]
    else:  # lake / reservoir / pond / bay / strait / sound / sea
        tags = [
            '["natural"="water"]',
            '["landuse"="reservoir"]',
            '["water"="lake"]',
            '["water"="reservoir"]',
            '["natural"="bay"]',
            '["natural"="strait"]',
            '["place"="sea"]',
            '["place"="ocean"]',
        ]

    clauses = []
    for tag in tags:
        clauses.append(f'  relation["name"="{escaped}"]{tag}({bbox});')
        clauses.append(f'  way["name"="{escaped}"]{tag}({bbox});')

    body = "\n".join(clauses)
    return f'[out:json][timeout:{OVERPASS_TIMEOUT}];\n(\n{body}\n);\nout geom;'


def _http_post(query: str) -> dict:
    """POST *query* to the Overpass API and return parsed JSON."""
    data = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(
        OVERPASS_URL,
        data=data,
        headers={"User-Agent": _USER_AGENT},
    )
    for attempt, delay in enumerate(RETRY_DELAYS + [None], 1):
        try:
            with urllib.request.urlopen(request, timeout=OVERPASS_TIMEOUT + 10) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as exc:
            if delay is None or attempt > MAX_RETRIES:
                raise RuntimeError(f"Overpass request failed after {attempt} attempt(s): {exc}") from exc
            print(f"  [retry {attempt}/{MAX_RETRIES}] {exc} — waiting {delay}s …", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError("Unreachable")  # pragma: no cover


def _nominatim_resolve(name: str) -> Optional[tuple[str, str]]:
    """Query Nominatim to find the canonical OSM name for a water body.

    Returns (canonical_name, feature_type) drawn from the first matching
    water-body result, or None if nothing suitable is found.
    """
    params = urllib.parse.urlencode({
        "q": name,
        "format": "json",
        "limit": "5",
        "namedetails": "1",
        "addressdetails": "0",
    })
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            results = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"  [nominatim] Request failed: {exc}", file=sys.stderr)
        return None

    for result in results:
        cls = result.get("class", "")
        typ = result.get("type", "")
        if (cls, typ) not in _WATER_OSM_CLASSES:
            continue
        canonical = result.get("namedetails", {}).get("name", "").strip()
        if not canonical:
            canonical = result.get("display_name", "").split(",")[0].strip()
        if canonical:
            resolved_type = "river" if cls == "waterway" else "lake"
            return canonical, resolved_type

    return None


def fetch_water_body(name: str, feature_type: str) -> tuple[dict, str, str]:
    """Query Overpass; fall back to Nominatim name resolution if needed.

    Returns (response_data, resolved_name, resolved_feature_type).
    """
    query = _build_query(name, feature_type)
    print(f"  Querying Overpass API for '{name}' ({feature_type}) …", file=sys.stderr)
    data = _http_post(query)

    if not data.get("elements"):
        print(f"  No results. Querying Nominatim to resolve canonical name …", file=sys.stderr)
        resolved = _nominatim_resolve(name)
        if resolved:
            canonical_name, canonical_type = resolved
            print(f"  Nominatim: '{name}' → '{canonical_name}' ({canonical_type})", file=sys.stderr)
            query = _build_query(canonical_name, canonical_type)
            data = _http_post(query)
            return data, canonical_name, canonical_type
        else:
            print(f"  Nominatim found no matching water body.", file=sys.stderr)

    return data, name, feature_type


# ── geometry helpers ───────────────────────────────────────────────────────────

def _coords_from_geometry(geometry: list[dict]) -> list[tuple[float, float]]:
    """Convert an Overpass geometry list to (lon, lat) tuples."""
    return [(pt["lon"], pt["lat"]) for pt in geometry]


def _format_coords(coords: list[tuple[float, float]]) -> str:
    """Render a coordinate list as a KML coordinate string."""
    return "\n".join(f"{lon},{lat},0" for lon, lat in coords)


# ── river assembly ─────────────────────────────────────────────────────────────

def _chain_segments(segments: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """
    Greedily chain way-segments end-to-end into the fewest continuous
    polylines.  Segments may be reversed if needed to connect.

    Returns a list of chained polylines (each is a list of (lon, lat) tuples).
    """
    if not segments:
        return []

    remaining = [list(s) for s in segments]
    chains: list[list[tuple[float, float]]] = []

    while remaining:
        chain = remaining.pop(0)
        changed = True
        while changed and remaining:
            changed = False
            for i, seg in enumerate(remaining):
                if chain[-1] == seg[0]:
                    chain.extend(seg[1:])
                    remaining.pop(i)
                    changed = True
                    break
                if chain[-1] == seg[-1]:
                    chain.extend(reversed(seg[:-1]))
                    remaining.pop(i)
                    changed = True
                    break
                if chain[0] == seg[-1]:
                    chain = seg + chain[1:]
                    remaining.pop(i)
                    changed = True
                    break
                if chain[0] == seg[0]:
                    chain = list(reversed(seg)) + chain[1:]
                    remaining.pop(i)
                    changed = True
                    break
        chains.append(chain)

    return chains


def build_river_placemarks(
    parent: Element,
    name: str,
    elements: list[dict],
    style_id: str,
) -> int:
    """Add river LineString Placemarks to *parent*.  Returns segment count."""
    all_segments: list[list[tuple[float, float]]] = []

    for el in elements:
        if el["type"] == "way":
            coords = _coords_from_geometry(el.get("geometry", []))
            if coords:
                all_segments.append(coords)
        elif el["type"] == "relation":
            for member in el.get("members", []):
                if member.get("type") == "way" and member.get("geometry"):
                    coords = _coords_from_geometry(member["geometry"])
                    if coords:
                        all_segments.append(coords)

    if not all_segments:
        return 0

    chains = _chain_segments(all_segments)

    for idx, chain in enumerate(chains, 1):
        pm = SubElement(parent, "Placemark")
        SubElement(pm, "name").text = name if idx == 1 else f"{name} ({idx})"
        SubElement(pm, "styleUrl").text = f"#{style_id}"
        ls = SubElement(pm, "LineString")
        SubElement(ls, "tessellate").text = "1"
        SubElement(ls, "coordinates").text = _format_coords(chain)

    return len(chains)


# ── lake assembly ──────────────────────────────────────────────────────────────

def _ring_from_members(members: list[dict], role: str) -> list[list[tuple[float, float]]]:
    """Collect all way geometries for *role* ('outer' / 'inner') in a relation."""
    segs = []
    for m in members:
        if m.get("type") == "way" and m.get("role") == role and m.get("geometry"):
            segs.append(_coords_from_geometry(m["geometry"]))
    return segs


def _close_ring(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if coords and coords[0] != coords[-1]:
        return coords + [coords[0]]
    return coords


def build_lake_placemarks(
    parent: Element,
    name: str,
    elements: list[dict],
    style_id: str,
) -> int:
    """Add lake Polygon Placemarks to *parent*.  Returns polygon count."""
    count = 0

    for el in elements:
        if el["type"] == "way":
            coords = _coords_from_geometry(el.get("geometry", []))
            if len(coords) < 3:
                continue
            coords = _close_ring(coords)
            pm = SubElement(parent, "Placemark")
            SubElement(pm, "name").text = name
            SubElement(pm, "styleUrl").text = f"#{style_id}"
            poly = SubElement(pm, "Polygon")
            SubElement(poly, "tessellate").text = "1"
            ob = SubElement(poly, "outerBoundaryIs")
            lr = SubElement(ob, "LinearRing")
            SubElement(lr, "coordinates").text = _format_coords(coords)
            count += 1

        elif el["type"] == "relation":
            outer_segs = _ring_from_members(el.get("members", []), "outer")
            inner_segs = _ring_from_members(el.get("members", []), "inner")

            outer_chains = _chain_segments(outer_segs)
            inner_chains = _chain_segments(inner_segs)

            if not outer_chains:
                continue

            for idx, outer in enumerate(outer_chains, 1):
                outer = _close_ring(outer)
                pm = SubElement(parent, "Placemark")
                SubElement(pm, "name").text = name if idx == 1 else f"{name} ({idx})"
                SubElement(pm, "styleUrl").text = f"#{style_id}"
                poly = SubElement(pm, "Polygon")
                SubElement(poly, "tessellate").text = "1"
                ob = SubElement(poly, "outerBoundaryIs")
                lr = SubElement(ob, "LinearRing")
                SubElement(lr, "coordinates").text = _format_coords(outer)

                for inner in inner_chains:
                    inner = _close_ring(inner)
                    ib = SubElement(poly, "innerBoundaryIs")
                    ilr = SubElement(ib, "LinearRing")
                    SubElement(ilr, "coordinates").text = _format_coords(inner)

                count += 1

    return count


# ── KML document construction ──────────────────────────────────────────────────

def _add_river_style(doc: Element, style_id: str) -> None:
    style = SubElement(doc, "Style", id=style_id)
    ls = SubElement(style, "LineStyle")
    SubElement(ls, "color").text = RIVER_LINE_COLOR
    SubElement(ls, "width").text = RIVER_LINE_WIDTH
    poly = SubElement(style, "PolyStyle")
    SubElement(poly, "fill").text = "0"


def _add_lake_style(doc: Element, style_id: str) -> None:
    style = SubElement(doc, "Style", id=style_id)
    ls = SubElement(style, "LineStyle")
    SubElement(ls, "color").text = LAKE_BORDER_COLOR
    SubElement(ls, "width").text = LAKE_BORDER_WIDTH
    poly = SubElement(style, "PolyStyle")
    SubElement(poly, "color").text = LAKE_FILL_COLOR
    SubElement(poly, "fill").text = "1"


def build_kml(name: str, elements: list[dict], feature_type: str) -> str:
    """Construct and return a pretty-printed KML string."""
    root = Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = SubElement(root, "Document")
    SubElement(doc, "name").text = name

    if feature_type == "river":
        style_id = "riverStyle"
        _add_river_style(doc, style_id)
        count = build_river_placemarks(doc, name, elements, style_id)
        feature_label = "river segment(s)"
    else:
        style_id = "lakeStyle"
        _add_lake_style(doc, style_id)
        count = build_lake_placemarks(doc, name, elements, style_id)
        feature_label = "polygon(s)"

    if count == 0:
        raise ValueError(
            f"No drawable geometry found for '{name}' as a {feature_type}.\n"
            "Try --type lake or --type river, or check that the name matches "
            "the OpenStreetMap spelling exactly."
        )

    print(f"  Built {count} {feature_label}.", file=sys.stderr)

    # Pretty-print via minidom
    raw = tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="UTF-8")
    # toprettyxml adds an XML declaration; re-encode to str
    return pretty.decode("utf-8")


# ── auto-detect feature type ───────────────────────────────────────────────────

_LAKE_KEYWORDS = re.compile(
    r"\b(lake|lac|reservoir|pond|étang|lagoon|bay|baie|gulf|sound|strait|détroit|inlet)\b",
    re.IGNORECASE,
)
_RIVER_KEYWORDS = re.compile(
    r"\b(river|rivière|creek|brook|stream|canal|canal|coulee|coulée|falls)\b",
    re.IGNORECASE,
)


def guess_feature_type(name: str) -> str:
    if _LAKE_KEYWORDS.search(name):
        return "lake"
    if _RIVER_KEYWORDS.search(name):
        return "river"
    return "river"  # default


# ── main ───────────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name).strip("_") + ".kml"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a high-definition KML file for a Canadian river or lake "
            "using OpenStreetMap data."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python water_body_kml.py "Peace River"
  python water_body_kml.py "Lake Athabasca"
  python water_body_kml.py "Fraser River" --output fraser.kml
  python water_body_kml.py "Great Bear Lake" --type lake
  python water_body_kml.py "Bow River" --type river --output bow.kml
""",
    )
    parser.add_argument("name", help="Name of the river or lake (match OSM spelling)")
    parser.add_argument(
        "--type",
        choices=["river", "lake"],
        default=None,
        dest="feature_type",
        help="Force feature type (auto-detected from name if omitted)",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output KML file path (default: <name>.kml in current directory)",
    )
    args = parser.parse_args()

    name: str = args.name.strip()
    feature_type: str = args.feature_type or guess_feature_type(name)

    print(f"\nWater Body KML Generator", file=sys.stderr)
    print(f"  Name        : {name}", file=sys.stderr)
    print(f"  Feature type: {feature_type}", file=sys.stderr)
    print("", file=sys.stderr)

    try:
        response, name, feature_type = fetch_water_body(name, feature_type)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path: str = args.output or sanitize_filename(name)
    print(f"  Output      : {output_path}", file=sys.stderr)

    elements = response.get("elements", [])
    print(f"  Received {len(elements)} OSM element(s).", file=sys.stderr)

    if not elements:
        print(
            f"\nNo OSM elements found for '{name}' ({feature_type}) in Canada.\n"
            "Suggestions:\n"
            "  • Check spelling against https://www.openstreetmap.org\n"
            "  • Try --type lake or --type river\n"
            "  • French names may differ: 'Rivière de la Paix' vs 'Peace River'",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        kml_str = build_kml(name, elements, feature_type)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(kml_str)

    total_coords = kml_str.count(",0\n")  # rough node count
    print(f"\nDone — wrote '{output_path}' (~{total_coords:,} coordinate points).", file=sys.stderr)


if __name__ == "__main__":
    main()
