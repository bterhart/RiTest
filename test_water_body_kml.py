#!/usr/bin/env python3
"""
Unit tests for water_body_kml.py.

These tests run fully offline using synthetic Overpass-shaped fixtures so
that the KML-building logic can be verified without a live network connection.
"""

import sys
import unittest
from xml.etree import ElementTree as ET

from water_body_kml import (
    _chain_segments,
    _close_ring,
    _format_coords,
    build_kml,
    guess_feature_type,
    sanitize_filename,
)

# ── Synthetic Overpass fixtures ────────────────────────────────────────────────

def _make_way_coords(pts):
    """Turn a list of (lon, lat) into Overpass geometry dicts."""
    return [{"lon": lon, "lat": lat} for lon, lat in pts]


RIVER_WAY_A = {
    "type": "way",
    "id": 1,
    "geometry": _make_way_coords([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]),
}
RIVER_WAY_B = {
    "type": "way",
    "id": 2,
    "geometry": _make_way_coords([(2.0, 0.0), (3.0, 0.0), (4.0, 0.0)]),
}
RIVER_WAY_C = {
    "type": "way",
    "id": 3,
    "geometry": _make_way_coords([(5.0, 0.0), (6.0, 0.0)]),  # disconnected segment
}

LAKE_WAY = {
    "type": "way",
    "id": 4,
    "geometry": _make_way_coords([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]),
}

LAKE_RELATION = {
    "type": "relation",
    "id": 5,
    "members": [
        {
            "type": "way",
            "role": "outer",
            "geometry": _make_way_coords([(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)]),
        },
        {
            "type": "way",
            "role": "inner",
            "geometry": _make_way_coords([(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5), (0.5, 0.5)]),
        },
    ],
}

RIVER_RELATION = {
    "type": "relation",
    "id": 6,
    "members": [
        {
            "type": "way",
            "role": "main_stream",
            "geometry": _make_way_coords([(10.0, 50.0), (11.0, 50.0), (12.0, 50.0)]),
        },
        {
            "type": "way",
            "role": "main_stream",
            "geometry": _make_way_coords([(12.0, 50.0), (13.0, 50.0)]),
        },
    ],
}


# ── helpers ────────────────────────────────────────────────────────────────────

def _parse_kml(kml_str):
    """Parse KML string and strip the default namespace for easy querying."""
    root = ET.fromstring(kml_str)
    # Strip namespace prefixes from tags so findall works without qualification.
    for el in root.iter():
        if el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]
    return root


# ── test cases ─────────────────────────────────────────────────────────────────

class TestGuessFeatureType(unittest.TestCase):
    def test_river_keywords(self):
        for name in ["Peace River", "Bow River", "Fraser River", "Moose Creek"]:
            self.assertEqual(guess_feature_type(name), "river", name)

    def test_lake_keywords(self):
        for name in ["Lake Athabasca", "Great Bear Lake", "Reservoir du Nord"]:
            self.assertEqual(guess_feature_type(name), "lake", name)

    def test_default_is_river(self):
        self.assertEqual(guess_feature_type("Unnamed Body"), "river")


class TestSanitizeFilename(unittest.TestCase):
    def test_spaces_replaced(self):
        self.assertEqual(sanitize_filename("Peace River"), "Peace_River.kml")

    def test_special_chars_replaced(self):
        name = sanitize_filename("Rivière de la Paix")
        self.assertTrue(name.endswith(".kml"))
        self.assertNotIn(" ", name)


class TestFormatCoords(unittest.TestCase):
    def test_round_trip(self):
        coords = [(1.5, 2.5), (3.0, 4.0)]
        result = _format_coords(coords)
        lines = result.strip().split("\n")
        self.assertEqual(lines[0], "1.5,2.5,0")
        self.assertEqual(lines[1], "3.0,4.0,0")


class TestCloseRing(unittest.TestCase):
    def test_already_closed(self):
        ring = [(0, 0), (1, 0), (1, 1), (0, 0)]
        self.assertEqual(_close_ring(ring), ring)

    def test_open_ring_closed(self):
        ring = [(0, 0), (1, 0), (1, 1)]
        closed = _close_ring(ring)
        self.assertEqual(closed[0], closed[-1])
        self.assertEqual(len(closed), 4)


class TestChainSegments(unittest.TestCase):
    def test_two_connected_segments(self):
        a = [(0.0, 0.0), (1.0, 0.0)]
        b = [(1.0, 0.0), (2.0, 0.0)]
        chains = _chain_segments([a, b])
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(chains[0]), 3)

    def test_reversed_segment_connected(self):
        a = [(0.0, 0.0), (1.0, 0.0)]
        b = [(2.0, 0.0), (1.0, 0.0)]  # reversed
        chains = _chain_segments([a, b])
        self.assertEqual(len(chains), 1)
        self.assertEqual(chains[0][0], (0.0, 0.0))
        self.assertEqual(chains[0][-1], (2.0, 0.0))

    def test_disconnected_segments(self):
        a = [(0.0, 0.0), (1.0, 0.0)]
        b = [(5.0, 0.0), (6.0, 0.0)]
        chains = _chain_segments([a, b])
        self.assertEqual(len(chains), 2)

    def test_three_connected_segments(self):
        a = [(0.0, 0.0), (1.0, 0.0)]
        b = [(1.0, 0.0), (2.0, 0.0)]
        c = [(2.0, 0.0), (3.0, 0.0)]
        chains = _chain_segments([a, b, c])
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(chains[0]), 4)

    def test_empty_input(self):
        self.assertEqual(_chain_segments([]), [])


class TestRiverKML(unittest.TestCase):
    def _kml(self, elements):
        return build_kml("Test River", elements, "river")

    def test_single_way_produces_linestring(self):
        kml = self._kml([RIVER_WAY_A])
        root = _parse_kml(kml)
        ls = root.findall(".//LineString")
        self.assertEqual(len(ls), 1)

    def test_connected_ways_chained_into_one_linestring(self):
        kml = self._kml([RIVER_WAY_A, RIVER_WAY_B])
        root = _parse_kml(kml)
        ls = root.findall(".//LineString")
        # The two connected segments should merge into a single LineString.
        self.assertEqual(len(ls), 1)

    def test_disconnected_way_produces_separate_linestring(self):
        kml = self._kml([RIVER_WAY_A, RIVER_WAY_C])
        root = _parse_kml(kml)
        ls = root.findall(".//LineString")
        self.assertEqual(len(ls), 2)

    def test_relation_members_used(self):
        kml = self._kml([RIVER_RELATION])
        root = _parse_kml(kml)
        ls = root.findall(".//LineString")
        self.assertGreaterEqual(len(ls), 1)
        coords_text = ls[0].find("coordinates").text
        self.assertIn("10.0,50.0", coords_text)

    def test_style_present(self):
        kml = self._kml([RIVER_WAY_A])
        root = _parse_kml(kml)
        styles = root.findall(".//Style")
        self.assertGreater(len(styles), 0)
        ls_color = root.find(".//LineStyle/color")
        self.assertIsNotNone(ls_color)

    def test_coordinate_count(self):
        kml = self._kml([RIVER_WAY_A])
        coords = root = _parse_kml(kml).find(".//coordinates").text.strip().split("\n")
        self.assertEqual(len(coords), 3)  # RIVER_WAY_A has 3 points

    def test_no_elements_raises(self):
        with self.assertRaises(ValueError):
            build_kml("Ghost River", [], "river")

    def test_kml_is_valid_xml(self):
        kml = self._kml([RIVER_WAY_A])
        # Should not raise
        ET.fromstring(kml)


class TestLakeKML(unittest.TestCase):
    def _kml(self, elements):
        return build_kml("Test Lake", elements, "lake")

    def test_simple_way_produces_polygon(self):
        kml = self._kml([LAKE_WAY])
        root = _parse_kml(kml)
        polys = root.findall(".//Polygon")
        self.assertEqual(len(polys), 1)

    def test_polygon_is_closed(self):
        kml = self._kml([LAKE_WAY])
        root = _parse_kml(kml)
        coords_text = root.find(".//LinearRing/coordinates").text.strip()
        lines = coords_text.split("\n")
        self.assertEqual(lines[0], lines[-1], "Polygon ring should be closed")

    def test_relation_outer_and_inner_boundary(self):
        kml = self._kml([LAKE_RELATION])
        root = _parse_kml(kml)
        self.assertIsNotNone(root.find(".//outerBoundaryIs"))
        self.assertIsNotNone(root.find(".//innerBoundaryIs"))

    def test_lake_style_has_poly_fill(self):
        kml = self._kml([LAKE_WAY])
        root = _parse_kml(kml)
        fill = root.find(".//PolyStyle/fill")
        self.assertIsNotNone(fill)
        self.assertEqual(fill.text, "1")

    def test_kml_is_valid_xml(self):
        kml = self._kml([LAKE_WAY])
        ET.fromstring(kml)

    def test_relation_polygon_coordinates_populated(self):
        kml = self._kml([LAKE_RELATION])
        root = _parse_kml(kml)
        outer_coords = root.find(".//outerBoundaryIs/LinearRing/coordinates").text.strip()
        self.assertIn("0.0,0.0", outer_coords)


if __name__ == "__main__":
    unittest.main(verbosity=2)
