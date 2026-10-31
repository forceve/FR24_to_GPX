# FR24 to GPX

Convert a Flightradar24-exported **KML Route** into a **GPX 1.1 track**, with optional time-based great-circle interpolation between recorded points.

## Requirements

- Python 3.10+
- `click` (install with `pip install -r requirements.txt`)

## Usage

Interactive mode (prompts for the input, output, interpolation interval and departure date):

```bash
python converter.py
```

Non-interactive mode:

```bash
python converter.py --input flight.kml --output flight.gpx --interval 30 --date 2025-01-15
```

- `--interval`: desired **maximum gap within each original segment**, in seconds. `0` disables interpolation and retains the exact original coordinates. Original sample points remain in the exported track. For example, a 100-second source gap and `--interval 30` produces timestamps at 0, 30, 60, 90, and 100 seconds.
- `--date`: optional flight departure **calendar date in UTC+8**, preserving every timestamp's time-of-day and shifting the entire track by whole days. Leave blank to keep original dates.
- All GPX timestamps are written in **UTC**, including those originally specified with a timezone offset. Subsecond precision is preserved.
- Input must contain a KML folder named `Route`, containing timestamped Point placemarks. A `Placemark` without both `<TimeStamp><when>` and `<Point><coordinates>` is skipped.

## Interpolation and data quality

Interpolated coordinates are calculated along a spherical great-circle arc (including across the International Date Line), using the elapsed-time fraction between two original observations. Elevation, when available at both endpoints, is interpolated linearly. Generated points are marked by the optional GPX extension:

```xml
<extensions><fr24:generated xmlns:fr24="https://github.com/forceve/FR24_to_GPX/ns/1">true</fr24:generated></extensions>
```

The tool warns if original points have gaps greater than **30 minutes**, or imply ground speeds greater than **1,400 km/h**. These heuristic thresholds only produce warnings; observations are not removed. Malformed coordinates, missing Route tracks, and non-increasing timestamps cause a clear error instead of writing misleading GPX output.

**Important:** Generated points are estimates, not new measurements. Straight great-circle interpolation cannot reconstruct turns, holding patterns, or other unobserved maneuvers. In particular, sparse FR24 samples may have stale heading and speed information. Forcing a very small interval does not increase the underlying accuracy.

## Run tests

```bash
python -m unittest discover -s tests -v
```

## Limitations

- Only Flightradar24's KML **Route folder / Point placemark** format is supported; other KML variants such as `gx:Track` are currently unsupported.
- Extremely rare antipodal point pairs have no unique great-circle arc; interpolation reports a clear error for such segments. Use `--interval 0` to preserve source observations without interpolation.
