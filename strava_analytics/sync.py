"""Incremental sync: activity summaries first (cheap), then per-run detail and streams."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import db
from .client import DailyLimitReached, StravaClient

RUN_TYPES = ("Run", "TrailRun", "VirtualRun")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def sync_summaries(conn: sqlite3.Connection, client: StravaClient, full: bool = False) -> int:
    """Pull the activity list. Incremental unless `full`, with a week of overlap."""
    after = None
    if not full:
        row = conn.execute("SELECT MAX(start_date) AS last FROM activities").fetchone()
        if row and row["last"]:
            after = _epoch(row["last"]) - 7 * 86400

    total = 0
    page = 1
    while True:
        batch = client.activities_page(page=page, after=after)
        if not batch:
            break
        total += db.upsert_summaries(conn, batch)
        print(f"  page {page}: {len(batch)} activities (total {total})")
        page += 1

    db.set_meta(conn, "last_summary_sync", _now())
    return total


def _pending(conn: sqlite3.Connection, column: str, limit: int) -> list[int]:
    placeholders = ",".join("?" * len(RUN_TYPES))
    rows = conn.execute(
        f"""
        SELECT id FROM activities
        WHERE {column} IS NULL
          AND sport_type IN ({placeholders})
          AND manual = 0
        ORDER BY start_date_local DESC
        LIMIT ?
        """,
        (*RUN_TYPES, limit),
    ).fetchall()
    return [r["id"] for r in rows]


def sync_details(conn: sqlite3.Connection, client: StravaClient, limit: int = 400) -> int:
    """Detailed fetch per run - this is what carries best_efforts (5k/10k/HM splits)."""
    pending = _pending(conn, "detail_fetched_at", limit)
    if not pending:
        print("  no runs pending detail")
        return 0
    done = 0
    for activity_id in pending:
        try:
            detail = client.activity_detail(activity_id)
        except DailyLimitReached as exc:
            print(f"  stopping: {exc}")
            break
        if detail is None:
            db.save_detail(conn, activity_id, {}, _now())
        else:
            db.save_detail(conn, activity_id, detail, _now())
        done += 1
        if done % 25 == 0:
            print(f"  details {done}/{len(pending)} (quota {client.daily_usage}/{client.daily_limit})")
    print(f"  details fetched: {done}")
    return done


def sync_streams(conn: sqlite3.Connection, client: StravaClient, limit: int = 300) -> int:
    """Per-second series. Needed for time-in-zone and aerobic decoupling."""
    pending = _pending(conn, "streams_fetched_at", limit)
    if not pending:
        print("  no runs pending streams")
        return 0
    done = 0
    for activity_id in pending:
        try:
            streams = client.activity_streams(activity_id)
        except DailyLimitReached as exc:
            print(f"  stopping: {exc}")
            break
        if streams:
            db.save_streams(conn, activity_id, streams, _now())
        else:
            db.mark_streams_missing(conn, activity_id, _now())
        done += 1
        if done % 25 == 0:
            print(f"  streams {done}/{len(pending)} (quota {client.daily_usage}/{client.daily_limit})")
    print(f"  streams fetched: {done}")
    return done


def sync_athlete_zones(conn: sqlite3.Connection, client: StravaClient) -> dict | None:
    try:
        zones = client.athlete_zones()
    except Exception:
        return None
    if zones:
        import json

        db.set_meta(conn, "athlete_zones", json.dumps(zones))
    return zones
