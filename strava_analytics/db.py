"""SQLite storage. One row per activity, plus best efforts and raw streams."""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT,
    type                 TEXT,
    sport_type           TEXT,
    start_date           TEXT,
    start_date_local     TEXT,
    timezone             TEXT,
    utc_offset           REAL,
    distance_m           REAL,
    moving_time_s        INTEGER,
    elapsed_time_s       INTEGER,
    total_elevation_gain REAL,
    average_speed        REAL,
    max_speed            REAL,
    average_heartrate    REAL,
    max_heartrate        REAL,
    has_heartrate        INTEGER,
    average_cadence      REAL,
    suffer_score         REAL,
    workout_type         INTEGER,
    trainer              INTEGER,
    commute              INTEGER,
    manual               INTEGER,
    gear_id              TEXT,
    kudos_count          INTEGER,
    achievement_count    INTEGER,
    calories             REAL,
    description          TEXT,
    detail_fetched_at    TEXT,
    streams_fetched_at   TEXT,
    raw_summary          TEXT,
    raw_detail           TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_local ON activities(start_date_local);
CREATE INDEX IF NOT EXISTS idx_activities_type  ON activities(sport_type);

CREATE TABLE IF NOT EXISTS best_efforts (
    activity_id      INTEGER,
    name             TEXT,
    distance_m       REAL,
    elapsed_time_s   INTEGER,
    moving_time_s    INTEGER,
    start_date_local TEXT,
    pr_rank          INTEGER,
    PRIMARY KEY (activity_id, name)
);

CREATE TABLE IF NOT EXISTS streams (
    activity_id INTEGER PRIMARY KEY,
    data        TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS plan_sessions (
    date        TEXT,
    title       TEXT,
    distance_km REAL,
    kind        TEXT,
    uid         TEXT PRIMARY KEY
);
"""

# Columns added after the first release; SQLite has no "ADD COLUMN IF NOT EXISTS".
MIGRATIONS = [
    ("activities", "original_distance_m", "REAL"),
    ("activities", "original_moving_time_s", "INTEGER"),
    ("activities", "distance_source", "TEXT"),
    ("plan_sessions", "description", "TEXT"),
    ("plan_sessions", "segments", "TEXT"),
    ("plan_sessions", "quality_km", "REAL"),
    ("plan_sessions", "easy_km", "REAL"),
    ("plan_sessions", "easy_ceiling_s", "REAL"),
    ("activities", "rpe", "REAL"),
    ("activities", "effort_override", "TEXT"),
    ("activities", "segment_km", "REAL"),
    ("activities", "segment_time_s", "INTEGER"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()

SUMMARY_COLUMNS = [
    "id", "name", "type", "sport_type", "start_date", "start_date_local", "timezone",
    "utc_offset", "distance_m", "moving_time_s", "elapsed_time_s", "total_elevation_gain",
    "average_speed", "max_speed", "average_heartrate", "max_heartrate", "has_heartrate",
    "average_cadence", "suffer_score", "workout_type", "trainer", "commute", "manual",
    "gear_id", "kudos_count", "achievement_count",
]


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _summary_row(activity: dict) -> tuple:
    get = activity.get
    values = {
        "id": get("id"),
        "name": get("name"),
        "type": get("type"),
        "sport_type": get("sport_type") or get("type"),
        "start_date": get("start_date"),
        "start_date_local": get("start_date_local"),
        "timezone": get("timezone"),
        "utc_offset": get("utc_offset"),
        "distance_m": get("distance"),
        "moving_time_s": get("moving_time"),
        "elapsed_time_s": get("elapsed_time"),
        "total_elevation_gain": get("total_elevation_gain"),
        "average_speed": get("average_speed"),
        "max_speed": get("max_speed"),
        "average_heartrate": get("average_heartrate"),
        "max_heartrate": get("max_heartrate"),
        "has_heartrate": int(bool(get("has_heartrate"))),
        "average_cadence": get("average_cadence"),
        "suffer_score": get("suffer_score"),
        "workout_type": get("workout_type"),
        "trainer": int(bool(get("trainer"))),
        "commute": int(bool(get("commute"))),
        "manual": int(bool(get("manual"))),
        "gear_id": get("gear_id"),
        "kudos_count": get("kudos_count"),
        "achievement_count": get("achievement_count"),
    }
    return tuple(values[c] for c in SUMMARY_COLUMNS) + (json.dumps(activity),)


def upsert_summaries(conn: sqlite3.Connection, activities: Iterable[dict]) -> int:
    cols = SUMMARY_COLUMNS + ["raw_summary"]
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    sql = (
        f"INSERT INTO activities ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}"
    )
    rows = [_summary_row(a) for a in activities]
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def save_detail(conn: sqlite3.Connection, activity_id: int, detail: dict, fetched_at: str) -> None:
    conn.execute(
        "UPDATE activities SET calories=?, description=?, detail_fetched_at=?, raw_detail=? WHERE id=?",
        (detail.get("calories"), detail.get("description"), fetched_at, json.dumps(detail), activity_id),
    )
    for effort in detail.get("best_efforts") or []:
        conn.execute(
            "INSERT OR REPLACE INTO best_efforts "
            "(activity_id, name, distance_m, elapsed_time_s, moving_time_s, start_date_local, pr_rank) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                activity_id,
                effort.get("name"),
                effort.get("distance"),
                effort.get("elapsed_time"),
                effort.get("moving_time"),
                effort.get("start_date_local"),
                effort.get("pr_rank"),
            ),
        )
    conn.commit()


def save_streams(conn: sqlite3.Connection, activity_id: int, streams: dict, fetched_at: str) -> None:
    payload = {k: v.get("data") for k, v in (streams or {}).items() if isinstance(v, dict)}
    conn.execute(
        "INSERT OR REPLACE INTO streams (activity_id, data) VALUES (?,?)",
        (activity_id, json.dumps(payload)),
    )
    conn.execute("UPDATE activities SET streams_fetched_at=? WHERE id=?", (fetched_at, activity_id))
    conn.commit()


def mark_streams_missing(conn: sqlite3.Connection, activity_id: int, fetched_at: str) -> None:
    """Record the attempt so we never re-request streams that do not exist."""
    conn.execute("UPDATE activities SET streams_fetched_at=? WHERE id=?", (fetched_at, activity_id))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN sport_type IN ('Run','TrailRun','VirtualRun') THEN 1 ELSE 0 END) AS runs,
               SUM(CASE WHEN detail_fetched_at IS NOT NULL THEN 1 ELSE 0 END) AS detailed,
               SUM(CASE WHEN streams_fetched_at IS NOT NULL THEN 1 ELSE 0 END) AS streamed,
               MIN(start_date_local) AS first,
               MAX(start_date_local) AS last
        FROM activities
        """
    ).fetchone()
    return dict(row)
