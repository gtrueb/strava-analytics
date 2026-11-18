"""Import a training plan from a shared calendar (.ics) so future sessions can be
projected alongside what has actually been run.

Written against a Runna export, but it only assumes iCalendar plus a distance
somewhere in the event title, so most plan calendars will work.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

from . import db

# Runna prefixes running sessions with a runner emoji and strength work with a
# weightlifter; the distance is the trailing "- 13 km" (German decimal comma is
# escaped by the ICS spec as "\,").
RUN_MARKERS = ("\U0001f3c3", "run", "lauf")
STRENGTH_MARKERS = ("\U0001f3cb", "kraft", "yoga", "pilates", "dehnung", "strength")
DISTANCE_RE = re.compile(r"([\d]+(?:[.,]\d+)?)\s*km\s*$", re.IGNORECASE)
ANY_DISTANCE_RE = re.compile(r"([\d]+(?:[.,]\d+)?)\s*km", re.IGNORECASE)


EASY_MARKERS = ("gesprächstempo", "gesprachstempo", "conversational", "easy", "locker")
REPEAT_BLOCK_RE = re.compile(r"wiederhole\s+die\s+folgenden\s+(\d+)\s*x", re.I)
REPEAT_LIST_RE = re.compile(r"(\d+)\s*(?:Wiederholungen von|x)\b", re.I)
SEG_DIST_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(km|m)\b", re.I)
SEG_PACE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*/\s*km")
SEG_KPH_RE = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*kph", re.I)
CEILING_RE = re.compile(r"nicht schneller als\s*(\d{1,2}):(\d{2})\s*/\s*km", re.I)
# Runna opens every description with a summary line: "Tempo - 8 km - 40 m - 50 m".
# It restates the session total, so counting it would double every workout.
HEADER_RE = re.compile(r"^.+\s-\s\d+(?:[.,]\d+)?\s*km\s*-\s", re.I)


def _segment_from_line(line: str) -> dict | None:
    """One prescribed piece of a workout: distance plus target pace, if any."""
    dist = SEG_DIST_RE.search(line)
    if not dist:
        return None
    value = float(dist.group(1).replace(",", "."))
    km = value / 1000.0 if dist.group(2).lower() == "m" else value
    if km <= 0 or km > 45:
        return None

    pace = None
    hit = SEG_PACE_RE.search(line)
    if hit:
        pace = int(hit.group(1)) * 60 + int(hit.group(2))
    else:
        kph = SEG_KPH_RE.search(line)
        if kph:
            pace = round(3600 / float(kph.group(1).replace(",", ".")))

    easy = any(marker in line.lower() for marker in EASY_MARKERS)
    # A conversational segment quoting a "no faster than" ceiling is still easy.
    if easy or pace is None:
        return {"km": round(km, 2), "pace_s": None, "easy": True}
    return {"km": round(km, 2), "pace_s": pace, "easy": False}


def parse_segments(description: str) -> tuple[list[dict], float | None]:
    """Expand a Runna workout description into a flat list of segments.

    Handles both repeat forms: "Wiederhole die folgenden 3x:" with a ---- delimited
    block, and "2 Wiederholungen von:" followed by bullet lines.
    """
    if not description:
        return [], None

    text = description.split("📲")[0]
    lines = [line.strip() for line in text.split("\n")]

    ceiling = None
    hit = CEILING_RE.search(text)
    if hit:
        ceiling = int(hit.group(1)) * 60 + int(hit.group(2))

    segments: list[dict] = []
    index = 0
    while index < len(lines) and not lines[index]:
        index += 1
    if index < len(lines) and HEADER_RE.match(lines[index]):
        index += 1

    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue

        block = REPEAT_BLOCK_RE.search(line)
        if block:
            repeats = int(block.group(1))
            index += 1
            while index < len(lines) and not lines[index].startswith("---"):
                index += 1
            index += 1  # step over the opening delimiter
            body = []
            while index < len(lines) and not lines[index].startswith("---"):
                if lines[index]:
                    body.append(lines[index])
                index += 1
            index += 1
            for _ in range(repeats):
                for entry in body:
                    seg = _segment_from_line(entry)
                    if seg:
                        segments.append(seg)
            continue

        listed = REPEAT_LIST_RE.match(line)
        if listed and "wiederholung" in line.lower():
            repeats = int(listed.group(1))
            index += 1
            body = []
            while index < len(lines) and lines[index].startswith(("•", "-", "*")):
                body.append(lines[index].lstrip("•-* "))
                index += 1
            for _ in range(repeats):
                for entry in body:
                    seg = _segment_from_line(entry)
                    if seg:
                        segments.append(seg)
            continue

        seg = _segment_from_line(line)
        if seg:
            segments.append(seg)
        index += 1

    return segments, ceiling


def _unfold(text: str) -> str:
    """Undo iCalendar line folding (a CRLF followed by a single space)."""
    return text.replace("\r\n ", "").replace("\r\n", "\n").replace("\n ", "")


def _unescape(text: str) -> str:
    return text.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n").strip()


def fetch(source: str) -> str:
    """Accept a raw .ics URL, a Google Calendar share link, or a local file."""
    if source.startswith(("http://", "https://")):
        url = source
        if "calendar.google.com" in url and "cid=" in url:
            import base64

            cid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["cid"][0]
            padding = "=" * (-len(cid) % 4)
            calendar_id = base64.b64decode(cid + padding).decode()
            quoted = urllib.parse.quote(calendar_id, safe="")
            url = f"https://calendar.google.com/calendar/ical/{quoted}/public/basic.ics"
        request = urllib.request.Request(url, headers={"User-Agent": "strava-analytics/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", "replace")
    return Path(source).expanduser().read_text(encoding="utf-8", errors="replace")


def parse(ics_text: str) -> tuple[list[dict], str | None]:
    text = _unfold(ics_text)
    name_match = re.search(r"X-WR-CALNAME:(.*)", text)
    plan_name = _unescape(name_match.group(1)) if name_match else None

    sessions = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        start = re.search(r"DTSTART;VALUE=DATE:(\d{8})", block)
        summary = re.search(r"SUMMARY:(.*)", block)
        uid_match = re.search(r"UID:(.*)", block)
        desc_match = re.search(r"DESCRIPTION:(.*)", block)
        if not start or not summary:
            # Completed activities synced back into the calendar carry a
            # timestamped DTSTART; only all-day entries are plan sessions.
            continue

        title = _unescape(summary.group(1))
        lowered = title.lower()
        if any(marker in lowered for marker in STRENGTH_MARKERS):
            kind = "strength"
        elif any(marker in lowered for marker in RUN_MARKERS):
            kind = "run"
        else:
            continue

        distance = None
        if kind == "run":
            # Prefer the trailing total ("... - 13 km") over an interval length
            # mentioned earlier in the name ("1 km Tempowechsel - 8 km").
            match = DISTANCE_RE.search(title) or ANY_DISTANCE_RE.search(title)
            if match:
                distance = float(match.group(1).replace(",", "."))
            if distance is None:
                continue
            if "rennen" in lowered or "race" in lowered:
                kind = "race"

        raw = start.group(1)
        iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        description = _unescape(desc_match.group(1)) if desc_match else ""
        segments, ceiling = parse_segments(description)
        sessions.append(
            {
                "date": iso,
                "title": title,
                "distance_km": distance,
                "kind": kind,
                "uid": _unescape(uid_match.group(1)) if uid_match else f"{iso}|{title}",
                "description": description.split("📲")[0].strip(),
                "segments": json.dumps(segments),
                "quality_km": round(sum(s["km"] for s in segments if not s["easy"]), 2),
                "easy_km": round(sum(s["km"] for s in segments if s["easy"]), 2),
                "easy_ceiling_s": ceiling,
            }
        )
    return sorted(sessions, key=lambda s: s["date"]), plan_name


def import_plan(source: str, conn=None) -> dict:
    close = conn is None
    conn = conn or db.connect()
    try:
        sessions, plan_name = parse(fetch(source))
        conn.execute("DELETE FROM plan_sessions")
        conn.executemany(
            "INSERT OR REPLACE INTO plan_sessions "
            "(date, title, distance_km, kind, uid, description, segments, quality_km, easy_km, easy_ceiling_s) "
            "VALUES (:date, :title, :distance_km, :kind, :uid, :description, :segments, "
            ":quality_km, :easy_km, :easy_ceiling_s)",
            sessions,
        )
        conn.commit()
        db.set_meta(conn, "plan_source", source)
        db.set_meta(conn, "plan_imported_at", datetime.now().isoformat(timespec="seconds"))

        runs = [s for s in sessions if s["kind"] in ("run", "race")]
        future = [s for s in runs if s["date"] >= str(date.today())]
        return {
            "plan_name": plan_name,
            "total": len(sessions),
            "runs": len(runs),
            "strength": sum(1 for s in sessions if s["kind"] == "strength"),
            "future_runs": len(future),
            "future_km": round(sum(s["distance_km"] or 0 for s in future), 1),
            "first": runs[0]["date"] if runs else None,
            "last": runs[-1]["date"] if runs else None,
        }
    finally:
        if close:
            conn.close()


def load_plan(conn=None) -> list[dict]:
    close = conn is None
    conn = conn or db.connect()
    try:
        rows = conn.execute(
            "SELECT date, title, distance_km, kind, description, segments, "
            "quality_km, easy_km, easy_ceiling_s FROM plan_sessions "
            "WHERE kind IN ('run','race') ORDER BY date"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if close:
            conn.close()
