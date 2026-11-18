"""Parse the activity files inside a Strava bulk export (.fit / .gpx / .tcx, gzipped or not).

The export ships the raw recording, which is strictly better than the API: full
per-second series with no rate limit. From those series we can compute our own
best efforts, so the personal-best charts work without any API access.
"""

from __future__ import annotations

import gzip
import io
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# Distances we look for inside every run, in metres.
# Faster than any human over a kilometre or more; anything quicker is a data seam.
MAX_SPEED = 7.0  # metres per second, about 2:23/km

EFFORT_TARGETS = {
    "1k": 1000.0,
    "5k": 5000.0,
    "10k": 10000.0,
    "15k": 15000.0,
    "Half-Marathon": 21097.5,
}


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    return open(path, "rb")


def parse_activity_file(path: Path) -> dict | None:
    """Return {time, distance, heartrate, altitude, cadence, latlng} streams, or None."""
    name = path.name.lower()
    try:
        if ".fit" in name:
            return _parse_fit(path)
        if ".gpx" in name:
            return _parse_gpx(path)
        if ".tcx" in name:
            return _parse_tcx(path)
    except Exception:
        return None
    return None


# ------------------------------------------------------------------------ fit


def _parse_fit(path: Path) -> dict | None:
    import fitdecode

    times, dist, hr, alt, cad, latlng = [], [], [], [], [], []
    with _open(path) as raw:
        data = raw.read()
    with fitdecode.FitReader(io.BytesIO(data)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage) or frame.name != "record":
                continue
            def get(field, _frame=frame):
                if not _frame.has_field(field):
                    return None
                value = _frame.get_value(field)
                # fitdecode hands back a tuple for array fields (some watches log
                # altitude and cadence that way). Take the first sample so every
                # series stays a list of plain numbers.
                if isinstance(value, (tuple, list)):
                    return value[0] if value else None
                return value

            stamp = get("timestamp")
            if stamp is None:
                continue
            times.append(stamp)
            dist.append(get("distance"))
            hr.append(get("heart_rate"))
            alt.append(get("enhanced_altitude") if frame.has_field("enhanced_altitude") else get("altitude"))
            cad.append(get("cadence"))
            lat, lon = get("position_lat"), get("position_long")
            latlng.append(None if lat is None or lon is None else [_semicircles(lat), _semicircles(lon)])

    if not times:
        return None
    return _finalise(times, dist, hr, alt, cad, latlng)


def _semicircles(value) -> float:
    return value * (180.0 / 2 ** 31)


# ------------------------------------------------------------------------ gpx


def _parse_gpx(path: Path) -> dict | None:
    with _open(path) as raw:
        tree = ET.parse(raw)
    root = tree.getroot()
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    points = root.findall(".//g:trkpt", ns)
    if not points:  # some files use no namespace
        points = root.findall(".//trkpt")
        ns = {}

    times, dist, hr, alt, cad, latlng = [], [], [], [], [], []
    total = 0.0
    prev = None
    for point in points:
        lat, lon = float(point.get("lat")), float(point.get("lon"))
        stamp = _findtext(point, "time", ns)
        if not stamp:
            continue
        times.append(_iso(stamp))
        if prev is not None:
            total += _haversine(prev[0], prev[1], lat, lon)
        prev = (lat, lon)
        dist.append(round(total, 2))
        ele = _findtext(point, "ele", ns)
        alt.append(float(ele) if ele else None)
        latlng.append([lat, lon])
        hr.append(_ext_value(point, "hr"))
        cad.append(_ext_value(point, "cad"))

    if not times:
        return None
    return _finalise(times, dist, hr, alt, cad, latlng)


def _findtext(node, tag, ns):
    if ns:
        found = node.find(f"g:{tag}", ns)
        if found is not None:
            return found.text
    found = node.find(tag)
    return found.text if found is not None else None


def _ext_value(point, suffix):
    for child in point.iter():
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag == suffix and child.text:
            try:
                return float(child.text)
            except ValueError:
                return None
    return None


# ------------------------------------------------------------------------ tcx


def _parse_tcx(path: Path) -> dict | None:
    with _open(path) as raw:
        tree = ET.parse(raw)
    root = tree.getroot()
    ns = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    points = root.findall(".//t:Trackpoint", ns)
    if not points:
        points = root.findall(".//Trackpoint")
        ns = {}

    times, dist, hr, alt, cad, latlng = [], [], [], [], [], []
    for point in points:
        stamp = _findtext_tcx(point, "Time", ns)
        if not stamp:
            continue
        times.append(_iso(stamp))
        d = _findtext_tcx(point, "DistanceMeters", ns)
        dist.append(float(d) if d else None)
        a = _findtext_tcx(point, "AltitudeMeters", ns)
        alt.append(float(a) if a else None)
        c = _findtext_tcx(point, "Cadence", ns)
        cad.append(float(c) if c else None)
        beats = None
        for child in point.iter():
            if child.tag.rsplit("}", 1)[-1] == "Value" and child.text:
                parent_ok = True
                if parent_ok:
                    try:
                        beats = float(child.text)
                    except ValueError:
                        beats = None
                    break
        hr.append(beats)
        lat = lon = None
        for child in point.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "LatitudeDegrees" and child.text:
                lat = float(child.text)
            elif tag == "LongitudeDegrees" and child.text:
                lon = float(child.text)
        latlng.append(None if lat is None else [lat, lon])

    if not times:
        return None
    return _finalise(times, dist, hr, alt, cad, latlng)


def _findtext_tcx(node, tag, ns):
    if ns:
        found = node.find(f"t:{tag}", ns)
        if found is not None:
            return found.text
    found = node.find(tag)
    return found.text if found is not None else None


# ----------------------------------------------------------------------- util


def _iso(text: str) -> datetime:
    text = text.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp/2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _finalise(times, dist, hr, alt, cad, latlng) -> dict:
    base = times[0]
    seconds = [int((t - base).total_seconds()) for t in times]

    # Some devices only log distance sporadically; carry the last known value.
    filled, last = [], 0.0
    for d in dist:
        if d is not None:
            last = float(d)
        filled.append(round(last, 2))

    return {
        "start": base,
        "time": seconds,
        "distance": filled,
        "heartrate": hr,
        "altitude": alt,
        "cadence": cad,
        "latlng": latlng,
    }


def summarise(streams: dict) -> dict:
    """Activity-level metrics derived from the raw series."""
    times, dist = streams["time"], streams["distance"]
    if not times:
        return {}
    elapsed = times[-1] - times[0]
    distance = dist[-1] if dist else 0.0

    # Moving time: ignore samples where we covered essentially nothing.
    moving = 0
    for i in range(1, len(times)):
        dt = times[i] - times[i-1]
        dd = dist[i] - dist[i-1]
        if dt <= 0 or dt > 60:
            continue
        if dd / dt > 0.5:  # slower than 0.5 m/s counts as stopped
            moving += dt

    hr = [h for h in streams.get("heartrate") or [] if h]
    alt = [a for a in streams.get("altitude") or [] if isinstance(a, (int, float))]
    gain = 0.0
    if alt:
        smooth = _smooth(alt, 15)
        for i in range(1, len(smooth)):
            step = smooth[i] - smooth[i-1]
            if step > 0:
                gain += step
    cad = [c for c in streams.get("cadence") or [] if c]

    return {
        "distance": round(distance, 1),
        "elapsed_time": elapsed,
        "moving_time": moving or elapsed,
        "total_elevation_gain": round(gain, 1),
        "average_heartrate": round(sum(hr) / len(hr), 1) if hr else None,
        "max_heartrate": max(hr) if hr else None,
        "has_heartrate": bool(hr),
        # FIT stores one-leg cadence; Strava reports the same figure, so keep it as is.
        "average_cadence": round(sum(cad) / len(cad), 1) if cad else None,
        "average_speed": round(distance / moving, 3) if moving else None,
    }


def _smooth(values: list[float], window: int) -> list[float]:
    if len(values) < window:
        return values
    out, running = [], sum(values[:window])
    out.extend([values[0]] * (window - 1))
    out.append(running / window)
    for i in range(window, len(values)):
        running += values[i] - values[i - window]
        out.append(running / window)
    return out[:len(values)]


def best_efforts(streams: dict) -> list[dict]:
    """Fastest time over each target distance - a two-pointer sweep over the series."""
    times, dist = streams["time"], streams["distance"]
    if not times or not dist or dist[-1] < 1000:
        return []

    out = []
    for name, target in EFFORT_TARGETS.items():
        if dist[-1] < target:
            continue
        best = None
        start = 0
        for end in range(len(dist)):
            while start < end and dist[end] - dist[start + 1] >= target:
                start += 1
            if dist[end] - dist[start] >= target:
                span = times[end] - times[start]
                # A file that holds two recordings jumps thousands of metres
                # between samples. Nobody runs faster than MAX_SPEED, so a window
                # that implies it is a seam in the data, not an effort.
                if span >= target / MAX_SPEED and (best is None or span < best):
                    best = span
        if best:
            out.append({"name": name, "distance": target, "elapsed_time": best, "moving_time": best})
    return out


def fastest_segment(streams: dict, target_m: float) -> int | None:
    """Fastest continuous run of `target_m` metres, in seconds.

    Same two-pointer sweep as best_efforts, but for an arbitrary distance so a
    prescribed "4 km at 5:05/km" can be checked against what was actually run.
    """
    times, dist = streams.get("time") or [], streams.get("distance") or []
    if not times or not dist or dist[-1] < target_m:
        return None

    best = None
    start = 0
    for end in range(len(dist)):
        while start < end and dist[end] - dist[start + 1] >= target_m:
            start += 1
        if dist[end] - dist[start] >= target_m:
            span = times[end] - times[start]
            if span >= target_m / MAX_SPEED and (best is None or span < best):
                best = span
    return best
