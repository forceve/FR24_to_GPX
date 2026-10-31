"""Regression tests for FR24 KML conversion and great-circle resampling."""

import math
import tempfile
import unittest
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner
from converter import (EXT_NS, GPX_NS, TrackPoint, convert, read_kml,
                       resample, setup)


def kml(items, folder='Route'):
    records = '\n'.join(
        f'<Placemark><TimeStamp><when>{stamp}</when></TimeStamp>'
        f'<Point><coordinates>{lon},{lat},{ele}</coordinates></Point></Placemark>'
        for stamp, lon, lat, ele in items
    )
    return (f'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2">'
            f'<Document><Folder><name>{folder}</name>{records}</Folder></Document></kml>')


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / 'flight.kml'
        self.dst = Path(self.tmp.name) / 'flight.gpx'

    def write(self, items):
        self.src.write_text(kml(items), encoding='utf-8')

    def trackpoints(self):
        return ET.parse(self.dst).getroot().findall(f'.//{{{GPX_NS}}}trkpt')

    def test_basic_conversion_and_utc_offset(self):
        self.write([('2025-01-10T08:00:00+08:00', '116', '39', '123.4'),
                    ('2025-01-10T00:02:00Z', '117', '40', '223.4')])
        self.assertEqual(convert(self.src, self.dst), (2, 2))
        root = ET.parse(self.dst).getroot()
        self.assertEqual(root.attrib['version'], '1.1')
        self.assertEqual(root.attrib['creator'], 'FR24_to_GPX')
        pts = self.trackpoints()
        self.assertEqual(pts[0].findtext(f'{{{GPX_NS}}}time'), '2025-01-10T00:00:00Z')
        self.assertEqual(pts[0].findtext(f'{{{GPX_NS}}}ele'), '123.4')

    def test_interval_uses_actual_seconds_and_keeps_endpoints(self):
        self.write([('2025-01-10T00:00:00Z', '0', '0', '100'),
                    ('2025-01-10T00:01:40Z', '0', '0', '200')])
        self.assertEqual(convert(self.src, self.dst, interval=30), (2, 5))
        pts = self.trackpoints()
        times = [p.findtext(f'{{{GPX_NS}}}time') for p in pts]
        self.assertEqual(times, [f'2025-01-10T00:00:{s:02d}Z' for s in (0, 30)] +
                         ['2025-01-10T00:01:00Z', '2025-01-10T00:01:30Z', '2025-01-10T00:01:40Z'])
        self.assertAlmostEqual(float(pts[1].findtext(f'{{{GPX_NS}}}ele')), 130.0)
        self.assertEqual([p.find(f'{{{GPX_NS}}}extensions') is not None for p in pts],
                         [False, True, True, True, False])
        generated = pts[1].findtext(f'{{{GPX_NS}}}extensions/{{{EXT_NS}}}generated')
        self.assertEqual(generated, 'true')

    def test_great_circle_midpoint_bends_north(self):
        self.write([('2025-01-10T00:00:00Z', '-90', '0', '0'),
                    ('2025-01-10T00:02:00Z', '90', '0', '0')])
        # Antipodal points do not uniquely determine a great-circle arc.
        with self.assertRaisesRegex(ValueError, 'antipodal'):
            convert(self.src, self.dst, interval=60)
        self.write([('2025-01-10T00:00:00Z', '-60', '45', '0'),
                    ('2025-01-10T00:02:00Z', '60', '45', '0')])
        convert(self.src, self.dst, interval=60)
        midpoint = self.trackpoints()[1]
        self.assertGreater(float(midpoint.attrib['lat']), 60)

    def test_antimeridian_wrap(self):
        self.write([('2025-01-10T00:00:00Z', '179', '10', '0'),
                    ('2025-01-10T00:02:00Z', '-179', '10', '0')])
        convert(self.src, self.dst, interval=60)
        self.assertAlmostEqual(abs(float(self.trackpoints()[1].attrib['lon'])), 180, places=5)

    def test_departure_date_local_utc8(self):
        self.write([('2025-01-10T17:00:00Z', '0', '0', '10')])
        convert(self.src, self.dst, date='2025-01-15')
        self.assertEqual(self.trackpoints()[0].findtext(f'{{{GPX_NS}}}time'),
                         '2025-01-14T17:00:00Z')

    def test_fractional_seconds_preserved(self):
        self.write([('2025-01-10T00:00:00.123456Z', '0', '0', '0')])
        convert(self.src, self.dst)
        self.assertEqual(self.trackpoints()[0].findtext(f'{{{GPX_NS}}}time'),
                         '2025-01-10T00:00:00.123456Z')

    def test_missing_route_and_no_points(self):
        self.src.write_text(kml([('2025-01-10T00:00:00Z', '0', '0', '0')], 'Other'))
        with self.assertRaisesRegex(ValueError, "Route"):
            convert(self.src, self.dst)
        self.src.write_text(kml([]))
        with self.assertRaisesRegex(ValueError, 'no timestamped'):
            convert(self.src, self.dst)

    def test_invalid_timestamps_and_coordinates(self):
        self.write([('invalid', '0', '0', '0')])
        with self.assertRaisesRegex(ValueError, 'timestamp'):
            convert(self.src, self.dst)
        self.write([('2025-01-10T00:00:00Z', '0', '91', '0')])
        with self.assertRaisesRegex(ValueError, 'Out-of-range'):
            convert(self.src, self.dst)

    def test_nonmonotonic_timestamps_and_invalid_interval(self):
        self.write([('2025-01-10T00:02:00Z', '0', '0', '0'),
                    ('2025-01-10T00:01:00Z', '0', '0', '0')])
        with self.assertRaisesRegex(ValueError, 'strictly increasing'):
            convert(self.src, self.dst)
        with self.assertRaisesRegex(ValueError, 'negative'):
            convert(self.src, self.dst, interval=-1)

    def test_large_gap_and_speed_warnings(self):
        self.write([('2025-01-10T00:00:00Z', '0', '0', '0'),
                    ('2025-01-10T00:40:00Z', '2', '0', '0')])
        with warnings.catch_warnings(record=True) as captures:
            warnings.simplefilter('always')
            convert(self.src, self.dst, interval=600)
        self.assertTrue(any('Large sampling gap' in str(w.message) for w in captures))
        self.write([('2025-01-10T00:00:00Z', '0', '0', '0'),
                    ('2025-01-10T00:01:00Z', '10', '0', '0')])
        with warnings.catch_warnings(record=True) as captures:
            warnings.simplefilter('always')
            convert(self.src, self.dst, interval=30)
        self.assertTrue(any('Unusually high ground speed' in str(w.message) for w in captures))

    def test_warn_without_interpolation(self):
        self.write([('2025-01-10T00:00:00Z', '0', '0', '0'),
                    ('2025-01-10T00:40:00Z', '2', '0', '0')])
        with warnings.catch_warnings(record=True) as captures:
            warnings.simplefilter('always')
            convert(self.src, self.dst, interval=0)
        self.assertTrue(any('Large sampling gap' in str(w.message) for w in captures))

    def test_cli_noninteractive(self):
        self.write([('2025-01-10T00:00:00Z', '0', '0', '1')])
        response = CliRunner().invoke(setup, ['--input', str(self.src), '--output', str(self.dst),
                                             '--interval', '0', '--date', ''])
        self.assertEqual(response.exit_code, 0, response.output)
        self.assertTrue(self.dst.exists())


if __name__ == '__main__':
    unittest.main()
