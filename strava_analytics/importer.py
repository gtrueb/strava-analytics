"""Import a Strava bulk export (the archive from Settings > Download your data).

Works on any account, free included, and needs no API access. The export's
activities.csv is localised - both the column headers and the date format follow
the language your Strava account was set to - so the parsing here is deliberately
tolerant rather than positional.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

from . import db
from .config import DATA_DIR, ensure_dirs
from .exportfiles import best_efforts, parse_activity_file, summarise

# Canonical field -> header fragments in the languages Strava exports.
HEADERS = {
    "activity_id": ["activity id", "aktivitats id", "aktivitatsid", "id der aktivitat",
                    "id de lactivite", "id de la actividad", "id attivita"],
    "date": ["activity date", "aktivitatsdatum", "datum der aktivitat", "date de lactivite",
             "fecha de la actividad", "data attivita"],
    "name": ["activity name", "name der aktivitat", "nom de lactivite",
             "nombre de la actividad", "nome attivita"],
    "type": ["activity type", "aktivitatsart", "aktivitatstyp", "art der aktivitat",
             "type dactivite", "tipo de actividad", "tipo di attivita"],
    "description": ["activity description", "aktivitatsbeschreibung", "beschreibung der aktivitat"],
    "elapsed_time": ["elapsed time", "verstrichene zeit", "temps ecoule", "tiempo transcurrido"],
    "moving_time": ["moving time", "bewegungszeit", "zeit in bewegung", "temps de deplacement",
                    "tiempo en movimiento"],
    "distance": ["distance", "distanz", "strecke", "distancia"],
    "max_heartrate": ["max heart rate", "maximale herzfrequenz", "max herzfrequenz",
                      "frequence cardiaque maximale", "frecuencia cardiaca maxima"],
    "average_heartrate": ["average heart rate", "durchschnittliche herzfrequenz",
                          "frequence cardiaque moyenne", "frecuencia cardiaca media"],
    # "Höhenzunahme" is gain; "Höhenunterschied" is the loss column in the same
    # export, so gain must be listed first and win on column order.
    "total_elevation_gain": ["elevation gain", "hohenzunahme", "hohenmeter", "hohengewinn",
                             "positiver hohenunterschied", "aufstieg", "anstieg",
                             "denivele positif", "desnivel positivo", "dislivello positivo"],
    "average_speed": ["average speed", "durchschnittliche geschwindigkeit",
                      "durchschnittsgeschwindigkeit", "vitesse moyenne"],
    "max_speed": ["max speed", "hochstgeschw", "maximale geschwindigkeit",
                  "hochstgeschwindigkeit"],
    "average_cadence": ["average cadence", "durchschnittliche trittfrequenz",
                        "durchschnittliche schrittfrequenz", "cadence moyenne"],
    "suffer_score": ["relative effort", "relative leistung", "relative anstrengung",
                     "effort relatif"],
    "filename": ["filename", "dateiname", "nom du fichier", "nombre del archivo"],
    "commute": ["commute", "pendeln", "arbeitsweg", "pendelfahrt", "trajet domicile"],
}

RUN_WORDS = ("run", "lauf", "course a pied", "carrera", "corsa", "hardlopen", "løb", "bieg")
TRAIL_WORDS = ("trail", "trailrun", "traillauf")

MONTHS = {}
for index, names in enumerate(
    [
        ("jan", "januar", "january", "janv", "ene", "enero", "gen"),
        ("feb", "februar", "february", "fevr", "febrero"),
        ("mar", "mar", "march", "marz", "mrz", "mars", "marzo"),
        ("apr", "april", "avr", "avril", "abr", "abril", "aprile"),
        ("may", "mai", "mayo", "maggio"),
        ("jun", "juni", "june", "juin", "junio", "giugno"),
        ("jul", "juli", "july", "juil", "julio", "luglio"),
        ("aug", "august", "aout", "ago", "agosto"),
        ("sep", "sept", "september", "septembre", "septiembre", "settembre"),
        ("oct", "okt", "october", "oktober", "octobre", "octubre", "ottobre"),
        ("nov", "november", "novembre", "noviembre"),
        ("dec", "dez", "dezember", "december", "decembre", "diciembre", "dicembre"),
    ],
    start=1,
):
    for token in names:
        MONTHS.setdefault(token, index)


def _norm(text: str) -> str:
    """Lowercase, strip accents and punctuation so headers compare across locales."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("ß", "ss").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    # Collapse the runs of spaces that punctuation leaves behind, so
    # "Max. Herzfrequenz" and "Max Herzfrequenz" compare equal.
    return re.sub(r"\s+", " ", text).strip()


def map_columns(header_row: list[str]) -> dict[str, list[int]]:
    """Every column index that matches each canonical field (duplicates are normal)."""
    normalised = [_norm(h) for h in header_row]
    mapping: dict[str, list[int]] = {}
    for field, fragments in HEADERS.items():
        exact, loose = [], []
        for index, header in enumerate(normalised):
            if not header:
                continue
            if header in fragments:
                exact.append(index)
            elif any(fragment in header for fragment in fragments):
                loose.append(index)
        # Exact headers always win. Otherwise "Distanz" would also claim
        # "Auf Steigung angepasste Distanz" (grade-adjusted, and larger), and
        # the max-picker below would silently use the wrong number.
        hits = exact or loose
        if hits:
            mapping[field] = hits
    return mapping


def _first(row: list[str], indices: list[int] | None):
    if not indices:
        return None
    for index in indices:
        if index < len(row) and str(row[index]).strip() != "":
            return row[index].strip()
    return None


def _number(row: list[str], indices: list[int] | None, pick_max: bool = False):
    if not indices:
        return None
    values = []
    for index in indices:
        if index >= len(row):
            continue
        raw = str(row[index]).strip().replace(",", ".")
        if not raw:
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    if not values:
        return None
    return max(values) if pick_max else values[0]


def parse_date(text: str) -> datetime | None:
    """Strava writes the local start time in the account's locale."""
    if not text:
        return None
    text = text.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    lowered = _norm(text)
    year = month = day = None

    named = re.search(r"(\d{1,2})\s+([a-z]{3,})\s+(\d{4})", lowered)
    if named and named.group(2) in MONTHS:
        day, month, year = int(named.group(1)), MONTHS[named.group(2)], int(named.group(3))
    if year is None:
        named = re.search(r"([a-z]{3,})\s+(\d{1,2})\s+(\d{4})", lowered)
        if named and named.group(1) in MONTHS:
            month, day, year = MONTHS[named.group(1)], int(named.group(2)), int(named.group(3))
    if year is None:
        numeric = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
        if numeric:
            first, second, year = int(numeric.group(1)), int(numeric.group(2)), int(numeric.group(3))
            # "." separators are European (day first); "/" is US (month first).
            if "." in numeric.group(0):
                day, month = first, second
            else:
                month, day = first, second
            if month > 12:
                day, month = month, day
    if year is None:
        iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if iso:
            year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
    if year is None:
        return None

    hour = minute = second = 0
    clock = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?", text, re.IGNORECASE)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        second = int(clock.group(3) or 0)
        suffix = (clock.group(4) or "").lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def classify_sport(raw: str) -> str:
    text = _norm(raw)
    if any(word in text for word in TRAIL_WORDS):
        return "TrailRun"
    if any(word in text for word in RUN_WORDS):
        return "Run"
    return raw or "Unknown"


TITLE_KM_RE = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*km", re.IGNORECASE)


def reconcile_titles(conn, min_ratio: float = 1.15, max_km: float = 45.0) -> int:
    """Repair runs whose recording stopped early (dead phone) using the title.

    Only ever corrects *upwards*: the title claiming less than was recorded is
    normally an interval name ("1 km über und unter dem Schwellentempo" on a 7 km
    session), not a distance, so upward-only avoids butchering those. Elapsed and
    moving time scale with the distance, which keeps the pace as recorded.
    """
    # No "already corrected" guard: once a distance matches its title the ratio
    # test simply fails, so this is naturally idempotent - and it survives a
    # re-import, which resets distance_m back to the CSV value.
    rows = conn.execute(
        "SELECT id, name, distance_m, moving_time_s, elapsed_time_s FROM activities "
        "WHERE distance_m > 0"
    ).fetchall()

    corrected = 0
    for row in rows:
        matches = TITLE_KM_RE.findall(row["name"] or "")
        if not matches:
            continue
        claimed_km = max(float(m.replace(",", ".")) for m in matches)
        recorded_km = row["distance_m"] / 1000.0
        if not (recorded_km * min_ratio < claimed_km <= max_km):
            continue

        scale = claimed_km / recorded_km
        conn.execute(
            """UPDATE activities SET
                 original_distance_m = distance_m,
                 original_moving_time_s = moving_time_s,
                 distance_m = ?,
                 moving_time_s = ?,
                 elapsed_time_s = ?,
                 distance_source = 'title'
               WHERE id = ?""",
            (
                claimed_km * 1000.0,
                int(round((row["moving_time_s"] or 0) * scale)) or None,
                int(round((row["elapsed_time_s"] or 0) * scale)) or None,
                row["id"],
            ),
        )
        corrected += 1
    conn.commit()
    return corrected


def locate_export(path: Path) -> Path:
    """Accept a .zip or an already-extracted folder; return the folder."""
    path = path.expanduser().resolve()
    if path.is_dir():
        return path
    if path.suffix.lower() == ".zip":
        ensure_dirs()
        target = DATA_DIR / "export"
        if target.exists() and any(target.iterdir()):
            print(f"  reusing already-extracted export at {target}")
            return target
        target.mkdir(parents=True, exist_ok=True)
        print(f"  extracting {path.name} -> {target}")
        with zipfile.ZipFile(path) as archive:
            archive.extractall(target)
        return target
    raise SystemExit(f"Not a zip or folder: {path}")


def find_activities_csv(root: Path) -> Path:
    candidates = list(root.rglob("activities.csv")) + list(root.rglob("Aktivitaten.csv"))
    candidates += [p for p in root.rglob("*.csv") if _norm(p.stem) in ("activities", "aktivitaten")]
    if not candidates:
        raise SystemExit(f"No activities.csv found under {root}")
    return sorted(candidates, key=lambda p: len(p.parts))[0]


def import_export(
    path: Path,
    with_files: bool = True,
    runs_only: bool = True,
    limit_files: int | None = None,
    dry_run: bool = False,
) -> dict:
    root = locate_export(Path(path))
    csv_path = find_activities_csv(root)
    print(f"  reading {csv_path.relative_to(root)}")

    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    with open(csv_path, newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)

    columns = map_columns(header)
    missing = [f for f in ("activity_id", "date", "type") if f not in columns]
    if missing:
        print("\nCould not identify these columns: " + ", ".join(missing))
        print("Headers found:\n  " + "\n  ".join(header[:40]))
        raise SystemExit("Unrecognised export format - send me the header line and I will map it.")

    print(f"  {len(rows)} rows, mapped {len(columns)} of {len(HEADERS)} known columns")
    if dry_run:
        for field, indices in sorted(columns.items()):
            print(f"    {field:22s} <- " + ", ".join(header[i] for i in indices))
        sample = rows[0] if rows else []
        if sample:
            print(f"    sample date: {_first(sample, columns.get('date'))!r} "
                  f"-> {parse_date(_first(sample, columns.get('date')) or '')}")
        return {"dry_run": True, "rows": len(rows)}

    activities, file_map, descriptions = [], {}, {}
    skipped_type = skipped_date = 0
    for row in rows:
        raw_id = _first(row, columns.get("activity_id"))
        if not raw_id:
            continue
        try:
            activity_id = int(float(raw_id))
        except ValueError:
            continue

        sport = classify_sport(_first(row, columns.get("type")) or "")
        if runs_only and sport not in ("Run", "TrailRun"):
            skipped_type += 1
            continue

        started = parse_date(_first(row, columns.get("date")) or "")
        if not started:
            skipped_date += 1
            continue

        distance = _number(row, columns.get("distance"), pick_max=True)
        moving = _number(row, columns.get("moving_time"))
        elapsed = _number(row, columns.get("elapsed_time"))
        commute_raw = (_first(row, columns.get("commute")) or "").strip().lower()

        activities.append(
            {
                "id": activity_id,
                "name": _first(row, columns.get("name")) or "Run",
                "type": sport,
                "sport_type": sport,
                "start_date": started.isoformat(),
                "start_date_local": started.isoformat(),
                "timezone": None,
                "utc_offset": None,
                "distance": distance,
                "moving_time": int(moving) if moving else (int(elapsed) if elapsed else None),
                "elapsed_time": int(elapsed) if elapsed else None,
                "total_elevation_gain": _number(row, columns.get("total_elevation_gain")),
                "average_speed": _number(row, columns.get("average_speed")),
                "max_speed": _number(row, columns.get("max_speed")),
                "average_heartrate": _number(row, columns.get("average_heartrate")),
                "max_heartrate": _number(row, columns.get("max_heartrate")),
                "has_heartrate": _number(row, columns.get("average_heartrate")) is not None,
                "average_cadence": _number(row, columns.get("average_cadence")),
                "suffer_score": _number(row, columns.get("suffer_score")),
                "workout_type": None,
                "trainer": False,
                "commute": commute_raw in ("true", "1", "wahr", "ja", "yes", "vrai"),
                "manual": False,
                "gear_id": None,
                "kudos_count": None,
                "achievement_count": None,
            }
        )
        filename = _first(row, columns.get("filename"))
        if filename:
            file_map[activity_id] = filename
        # Runna writes the prescribed workout into the activity description, which
        # is how already-completed sessions can be checked against their targets.
        note = _first(row, columns.get("description"))
        if note and note.strip():
            descriptions[activity_id] = note.strip()

    # Distances are exported in kilometres in the summary column and metres in the
    # raw one; if everything looks small we clearly got kilometres.
    values = [a["distance"] for a in activities if a["distance"]]
    if values:
        values.sort()
        median = values[len(values)//2]
        if median < 500:
            print(f"  distance column looks like kilometres (median {median:.1f}) - converting to metres")
            for a in activities:
                if a["distance"]:
                    a["distance"] *= 1000

    conn = db.connect()
    stored = db.upsert_summaries(conn, activities)
    for activity_id, note in descriptions.items():
        conn.execute("UPDATE activities SET description=? WHERE id=?", (note, activity_id))
    conn.commit()
    print(f"  imported {stored} runs (skipped {skipped_type} non-runs, {skipped_date} undated)")

    parsed = efforts_written = 0
    if with_files and file_map:
        now = datetime.now().isoformat(timespec="seconds")
        already = {
            row["id"] for row in
            conn.execute("SELECT id FROM activities WHERE streams_fetched_at IS NOT NULL")
        }
        pending = [(aid, name) for aid, name in file_map.items() if aid not in already]
        if limit_files:
            pending = pending[:limit_files]
        print(f"  parsing {len(pending)} activity files (streams + best efforts)...")
        for index, (activity_id, filename) in enumerate(pending, 1):
            file_path = root / filename
            if not file_path.exists():
                matches = list(root.rglob(Path(filename).name))
                if not matches:
                    db.mark_streams_missing(conn, activity_id, now)
                    continue
                file_path = matches[0]

            streams = parse_activity_file(file_path)
            if not streams:
                db.mark_streams_missing(conn, activity_id, now)
                continue

            payload = {k: {"data": v} for k, v in streams.items() if k != "start"}
            db.save_streams(conn, activity_id, payload, now)
            parsed += 1

            efforts = best_efforts(streams)
            if efforts:
                local = conn.execute(
                    "SELECT start_date_local FROM activities WHERE id=?", (activity_id,)
                ).fetchone()[0]
                for effort in efforts:
                    effort["start_date_local"] = local
                db.save_detail(conn, activity_id, {"best_efforts": efforts}, now)
                efforts_written += len(efforts)

            # Fill in anything the CSV did not carry, from the recording itself.
            derived = summarise(streams)
            if derived:
                conn.execute(
                    """UPDATE activities SET
                         average_heartrate = COALESCE(average_heartrate, ?),
                         max_heartrate     = COALESCE(max_heartrate, ?),
                         has_heartrate     = COALESCE(NULLIF(has_heartrate,0), ?),
                         average_cadence   = COALESCE(average_cadence, ?),
                         total_elevation_gain = COALESCE(total_elevation_gain, ?),
                         moving_time_s     = COALESCE(moving_time_s, ?),
                         distance_m        = COALESCE(distance_m, ?)
                       WHERE id = ?""",
                    (derived.get("average_heartrate"), derived.get("max_heartrate"),
                     int(bool(derived.get("has_heartrate"))), derived.get("average_cadence"),
                     derived.get("total_elevation_gain"), derived.get("moving_time"),
                     derived.get("distance"), activity_id),
                )
                conn.commit()

            if index % 50 == 0:
                print(f"    {index}/{len(pending)} files")

    corrected = reconcile_titles(conn)
    if corrected:
        print(f"  distance corrected from the title on {corrected} run(s) with truncated GPS")

    db.set_meta(conn, "last_import", datetime.now().isoformat(timespec="seconds"))
    db.set_meta(conn, "import_source", str(root))
    summary = dict(db.counts(conn), files_parsed=parsed, best_efforts=efforts_written)
    conn.close()
    return summary
