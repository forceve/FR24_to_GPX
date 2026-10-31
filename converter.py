"""Convert Flightradar24 Route KML placemarks into a GPX 1.1 track."""

from __future__ import annotations

import math
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

GPX_NS = "http://www.topografix.com/GPX/1/1"
EXT_NS = "https://github.com/forceve/FR24_to_GPX/ns/1"
CST = timezone(timedelta(hours=8))
ET.register_namespace("", GPX_NS)
ET.register_namespace("fr24", EXT_NS)


@dataclass(frozen=True)
class TrackPoint:
    latitude: float
    longitude: float
    elevation: float | None
    timestamp: datetime
    generated: bool = False


def _parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid KML timestamp: {value!r}") from exc
    if result.tzinfo is None:
        # A KML TimeStamp without an explicit offset is treated as UTC.
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def read_kml(path: str | Path) -> list[TrackPoint]:
    """Read timestamped Point placemarks inside a folder named Route."""
    root = ET.parse(path).getroot()
    points: list[TrackPoint] = []
    route_folders = [
        folder for folder in root.findall(".//{*}Folder")
        if (folder.findtext("{*}name") or "").strip() == "Route"
    ]
    if not route_folders:
        raise ValueError("No KML folder named 'Route' was found")

    for folder in route_folders:
        for placemark in folder.findall(".//{*}Placemark"):
            time_node = placemark.find(".//{*}TimeStamp/{*}when")
            coord_node = placemark.find(".//{*}Point/{*}coordinates")
            if time_node is None or coord_node is None:
                # Non-track placemarks (e.g., labels) may be included in Route.
                continue
            if not time_node.text or not coord_node.text:
                raise ValueError("Empty timestamp or coordinates in Route placemark")
            parts = coord_node.text.strip().split(",")
            if len(parts) not in (2, 3):
                raise ValueError(f"Invalid KML Point coordinates: {coord_node.text!r}")
            try:
                lon, lat = (float(item.strip()) for item in parts[:2])
                ele = float(parts[2].strip()) if len(parts) == 3 else None
            except ValueError as exc:
                raise ValueError(f"Non-numeric KML coordinates: {coord_node.text!r}") from exc
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"Out-of-range KML coordinates: {lat}, {lon}")
            if not all(math.isfinite(x) for x in (lat, lon, *(() if ele is None else (ele,)))):
                raise ValueError("KML coordinates must be finite")
            points.append(TrackPoint(lat, lon, ele, _parse_time(time_node.text)))

    if not points:
        raise ValueError("The Route folder contains no timestamped Point placemarks")
    for a, b in zip(points, points[1:]):
        if b.timestamp <= a.timestamp:
            raise ValueError("Route timestamps must be strictly increasing")
    return points


def _unit_vector(latitude: float, longitude: float) -> tuple[float, float, float]:
    phi, lam = math.radians(latitude), math.radians(longitude)
    return (math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))


def _great_circle(a: TrackPoint, b: TrackPoint, fraction: float) -> tuple[float, float]:
    """Spherical linear interpolation; robust around the antimeridian."""
    u, v = _unit_vector(a.latitude, a.longitude), _unit_vector(b.latitude, b.longitude)
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v))))
    omega = math.acos(dot)
    if omega < 1e-10:
        return a.latitude, a.longitude
    if math.pi - omega < 1e-8:
        raise ValueError("Cannot interpolate between antipodal coordinates")
    sin_omega = math.sin(omega)
    s = math.sin((1 - fraction) * omega) / sin_omega
    t = math.sin(fraction * omega) / sin_omega
    x, y, z = (s * u[i] + t * v[i] for i in range(3))
    lat = math.degrees(math.atan2(z, math.hypot(x, y)))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon


def _distance_km(a: TrackPoint, b: TrackPoint) -> float:
    u, v = _unit_vector(a.latitude, a.longitude), _unit_vector(b.latitude, b.longitude)
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v))))
    return 6371.0088 * math.acos(dot)


def resample(points: list[TrackPoint], interval: int) -> list[TrackPoint]:
    """Insert points at interval-second offsets in every gap, retaining observations."""
    if interval < 0:
        raise ValueError("Interpolation interval cannot be negative")
    if interval == 0:
        return list(points)
    if not points:
        return []
    output = [points[0]]
    for a, b in zip(points, points[1:]):
        seconds = (b.timestamp - a.timestamp).total_seconds()
        if seconds <= 0:
            raise ValueError("Route timestamps must be strictly increasing")
        for step in range(1, math.ceil(seconds / interval)):
            elapsed = step * interval
            fraction = elapsed / seconds
            lat, lon = _great_circle(a, b, fraction)
            elevation = (
                a.elevation + fraction * (b.elevation - a.elevation)
                if a.elevation is not None and b.elevation is not None else None
            )
            output.append(TrackPoint(lat, lon, elevation,
                                     a.timestamp + timedelta(seconds=elapsed), True))
        output.append(b)
    return output


def _warn_anomalies(points: list[TrackPoint]) -> None:
    """Report suspicious source observations without discarding them."""
    for a, b in zip(points, points[1:]):
        seconds = (b.timestamp - a.timestamp).total_seconds()
        if seconds > 1800:
            warnings.warn(f"Large sampling gap: {seconds:.0f}s; interpolation is an estimate", stacklevel=2)
        speed_kmh = _distance_km(a, b) * 3600 / seconds
        if speed_kmh > 1400:
            warnings.warn(f"Unusually high ground speed: {speed_kmh:.0f} km/h", stacklevel=2)


def _shift_departure_date(points: list[TrackPoint], date: str) -> list[TrackPoint]:
    if not date:
        return points
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Departure date must be in YYYY-MM-DD format") from exc
    local_first_date = points[0].timestamp.astimezone(CST).date()
    delta = timedelta(days=(target_date - local_first_date).days)
    return [replace(p, timestamp=p.timestamp + delta) for p in points]


def write_gpx(points: list[TrackPoint], path: str | Path) -> None:
    root = ET.Element(f"{{{GPX_NS}}}gpx", {"version": "1.1", "creator": "FR24_to_GPX"})
    track = ET.SubElement(root, f"{{{GPX_NS}}}trk")
    segment = ET.SubElement(track, f"{{{GPX_NS}}}trkseg")
    for point in points:
        trkpt = ET.SubElement(segment, f"{{{GPX_NS}}}trkpt", {
            "lat": str(point.latitude), "lon": str(point.longitude)
        })
        if point.elevation is not None:
            ET.SubElement(trkpt, f"{{{GPX_NS}}}ele").text = str(point.elevation)
        utc_time = point.timestamp.astimezone(timezone.utc)
        precision = ("microseconds" if utc_time.microsecond % 1000 else
                     "milliseconds" if utc_time.microsecond else "seconds")
        text_time = utc_time.isoformat(timespec=precision)
        ET.SubElement(trkpt, f"{{{GPX_NS}}}time").text = text_time.replace("+00:00", "Z")
        if point.generated:
            extension = ET.SubElement(trkpt, f"{{{GPX_NS}}}extensions")
            ET.SubElement(extension, f"{{{EXT_NS}}}generated").text = "true"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def convert(kml_file: str | Path, gpx_file: str | Path,
            interval: int = 0, date: str = "") -> tuple[int, int]:
    """Convert one FR24 KML file, returning (original count, output count)."""
    if interval < 0:
        raise ValueError("Interpolation interval cannot be negative")
    points = read_kml(kml_file)
    points = _shift_departure_date(points, date)
    _warn_anomalies(points)
    output = resample(points, interval)
    write_gpx(output, gpx_file)
    return len(points), len(output)


@click.command()
@click.option("--input", "kml_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "gpx_file", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--interval", type=click.IntRange(min=0))
@click.option("--date", default=None, help="Departure date in UTC+8 (YYYY-MM-DD).")
def setup(kml_file: Path | None, gpx_file: Path | None,
          interval: int | None, date: str | None) -> None:
    """Convert FR24 Route KML to GPX. Missing options prompt interactively."""
    try:
        if kml_file is None:
            entered = click.prompt("Please enter the KML file path").strip().strip("\"'")
            kml_file = Path(entered)
        if not kml_file.is_file():
            raise click.ClickException(f"Input file does not exist: {kml_file}")
        if gpx_file is None:
            gpx_file = Path(click.prompt("GPX output path", default=str(kml_file.with_suffix(".gpx"))))
        if interval is None:
            interval = click.prompt("Interpolation interval in seconds (0 = no interpolation)", default=0, type=click.IntRange(min=0))
        if date is None:
            date = click.prompt("Departure date in UTC+8 (YYYY-MM-DD; blank to keep original)", default="", show_default=False)
        original, total = convert(kml_file, gpx_file, interval, date)
    except (ValueError, ET.ParseError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Converted {original} original points to {total} GPX points: {gpx_file}")


if __name__ == "__main__":
    setup()
