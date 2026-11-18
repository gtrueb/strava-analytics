#!/usr/bin/env python3
"""Command line entry point.  Use ./run <command>."""

from __future__ import annotations

import argparse
import sys

from strava_analytics import db
from strava_analytics.client import DailyLimitReached, StravaClient


def cmd_auth(args) -> None:
    from strava_analytics.auth import interactive_authorize

    print(
        "Note: since 30 June 2026 the Strava API requires a paid Strava subscription.\n"
        "Without one, use the free bulk export instead:  ./run import <export.zip>\n"
    )
    interactive_authorize()
    conn = db.connect()
    client = StravaClient()
    me = client.athlete()
    if me:
        print(f"Connected: {me.get('firstname')} {me.get('lastname')} - {me.get('city') or ''}")
    from strava_analytics.sync import sync_athlete_zones

    sync_athlete_zones(conn, client)
    print("\nNext:  ./run sync")


def cmd_status(args) -> None:
    conn = db.connect()
    c = db.counts(conn)
    if not c["total"]:
        print("Database is empty. Run:  ./run sync")
        return
    print(f"activities : {c['total']}  ({c['runs']} runs)")
    print(f"date range : {(c['first'] or '')[:10]} -> {(c['last'] or '')[:10]}")
    print(f"detail     : {c['detailed']}/{c['runs']} runs")
    print(f"streams    : {c['streamed']}/{c['runs']} runs")
    print(f"last sync  : {db.get_meta(conn, 'last_summary_sync', 'never')}")


def cmd_sync(args) -> None:
    conn = db.connect()
    client = StravaClient()
    try:
        print("Syncing activity summaries...")
        sync = __import__("strava_analytics.sync", fromlist=["sync"])
        sync.sync_summaries(conn, client, full=args.full)
        if not args.no_details:
            print("Fetching run details (best efforts)...")
            sync.sync_details(conn, client, limit=args.detail_limit)
        if not args.no_streams:
            print("Fetching run streams (per-second data)...")
            sync.sync_streams(conn, client, limit=args.stream_limit)
    except DailyLimitReached as exc:
        print(f"\n{exc}")
    finally:
        print(f"\nAPI requests used this run: {client.requests_made} "
              f"(daily quota {client.daily_usage}/{client.daily_limit})")
        cmd_status(args)


def cmd_import(args) -> None:
    from pathlib import Path

    from strava_analytics.importer import import_export

    print(f"Importing Strava export from {args.path}")
    summary = import_export(
        Path(args.path),
        with_files=not args.no_files,
        runs_only=not args.all_sports,
        limit_files=args.limit_files,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return
    print(
        f"\nDone: {summary['total']} activities, {summary['runs']} runs, "
        f"{summary['files_parsed']} files parsed, {summary['best_efforts']} best efforts computed"
    )
    print("Next:  ./run dashboard")


def cmd_plan(args) -> None:
    from strava_analytics.plan import import_plan

    print(f"Importing training plan from {args.source}")
    info = import_plan(args.source)
    print(f"  plan        : {info['plan_name'] or 'unnamed'}")
    print(f"  sessions    : {info['runs']} runs, {info['strength']} strength")
    print(f"  date range  : {info['first']} -> {info['last']}")
    print(f"  still ahead : {info['future_runs']} runs, {info['future_km']} km")
    print("\nNext:  ./run dashboard")


def cmd_add_run(args) -> None:
    """Log a run that has no GPS file - treadmill sessions above all."""
    from datetime import datetime

    from strava_analytics.analysis import parse_duration

    seconds = parse_duration(args.time)
    if not seconds:
        raise SystemExit("Could not read --time. Use mm:ss or h:mm:ss.")
    started = datetime.fromisoformat(f"{args.date}T{args.start}")

    conn = db.connect()
    # Manual entries take negative ids so they can never collide with a Strava id.
    base = -int(args.date.replace("-", ""))
    while conn.execute("SELECT 1 FROM activities WHERE id=?", (base,)).fetchone():
        base -= 1

    conn.execute(
        """INSERT INTO activities
           (id, name, type, sport_type, start_date, start_date_local, distance_m,
            moving_time_s, elapsed_time_s, has_heartrate, manual, trainer, commute,
            distance_source, rpe, effort_override)
           VALUES (?,?,?,?,?,?,?,?,?,0,1,?,0,'manual',?,?)""",
        (base, args.name, "Run", "Run", started.isoformat(), started.isoformat(),
         args.km * 1000.0, seconds, seconds, 1 if args.treadmill else 0,
         args.rpe, args.effort),
    )
    conn.commit()
    pace = seconds / args.km
    print(f"Added: {args.date}  {args.km:.2f} km in {seconds//60}:{seconds%60:02d} "
          f"({int(pace//60)}:{int(pace%60):02d}/km)  id {base}")
    print("Next:  ./run dashboard")


def cmd_set_effort(args) -> None:
    """Attach a session RPE to an already-logged run."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, name, distance_m/1000.0 AS km FROM activities "
        "WHERE substr(start_date_local,1,10)=?",
        (args.date,),
    ).fetchall()
    if not rows:
        raise SystemExit(f"No activity on {args.date}")

    from strava_analytics.analysis import parse_duration

    seg_s = parse_duration(args.segment_time) if args.segment_time else None
    for row in rows:
        conn.execute(
            "UPDATE activities SET rpe=?, effort_override=?, segment_km=?, segment_time_s=? "
            "WHERE id=?",
            (args.rpe, args.effort, args.segment_km, seg_s, row["id"]),
        )
        bits = []
        if args.effort:
            bits.append(args.effort)
        if args.rpe:
            bits.append(f"RPE {args.rpe:g}")
        if seg_s and args.segment_km:
            pace = seg_s / args.segment_km
            bits.append(f"best {args.segment_km:g} km in {seg_s//60}:{seg_s%60:02d} "
                        f"({int(pace//60)}:{int(pace%60):02d}/km)")
        print(f"  {args.date}  {row['km']:.2f} km  {row['name']}  -> {', '.join(bits) or 'cleared'}")
    conn.commit()
    print("\nNext:  ./run dashboard")


def cmd_dashboard(args) -> None:
    from strava_analytics.dashboard import build_dashboard

    path = build_dashboard(open_browser=not args.no_open, web=args.web)
    print(f"Dashboard written to {path}")


def cmd_races(args) -> None:
    import json

    from strava_analytics.analysis import list_candidate_races

    races = list_candidate_races()
    if not races:
        print("No race-like activities found (nothing over 20 km, no race keywords in names).")
        return
    for race in races:
        flag = " [flagged as race]" if race["is_race_flag"] else ""
        print(f"{race['date']}  {race['distance_km']:5.2f} km  {race['time']:>8}  {race['name']}{flag}")

    half = [r for r in races if 20.0 <= r["distance_km"] <= 23.0]
    if half:
        print("\nTo pin these as your races, put this in profile.json under \"races\":")
        print(json.dumps(
            [{"name": r["name"], "date": r["date"], "distance_km": r["distance_km"], "time": r["time"]}
             for r in half],
            indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(prog="run", description="Personal Strava training analytics")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="authorise this app against your Strava account").set_defaults(func=cmd_auth)
    sub.add_parser("status", help="what is in the local database").set_defaults(func=cmd_status)

    p_sync = sub.add_parser("sync", help="pull new data from Strava")
    p_sync.add_argument("--full", action="store_true", help="re-scan the whole history, not just new activities")
    p_sync.add_argument("--no-details", action="store_true")
    p_sync.add_argument("--no-streams", action="store_true")
    p_sync.add_argument("--detail-limit", type=int, default=400)
    p_sync.add_argument("--stream-limit", type=int, default=300)
    p_sync.set_defaults(func=cmd_sync)

    p_imp = sub.add_parser("import", help="import a Strava bulk export (.zip or folder)")
    p_imp.add_argument("path", help="path to the export .zip or the extracted folder")
    p_imp.add_argument("--no-files", action="store_true", help="skip .fit/.gpx parsing (much faster, no streams or PBs)")
    p_imp.add_argument("--all-sports", action="store_true", help="import rides and swims too, not only runs")
    p_imp.add_argument("--limit-files", type=int, default=None)
    p_imp.add_argument("--dry-run", action="store_true", help="show the detected column mapping and stop")
    p_imp.set_defaults(func=cmd_import)

    p_plan = sub.add_parser("plan", help="import a training plan from an .ics calendar or share link")
    p_plan.add_argument("source", help="calendar URL (Google share link or .ics) or a local .ics file")
    p_plan.set_defaults(func=cmd_plan)

    p_add = sub.add_parser("add-run", help="log a run with no GPS file (treadmill, watch failure)")
    p_add.add_argument("--date", required=True, help="YYYY-MM-DD")
    p_add.add_argument("--km", type=float, required=True)
    p_add.add_argument("--time", required=True, help="mm:ss or h:mm:ss")
    p_add.add_argument("--name", default="Treadmill run")
    p_add.add_argument("--start", default="18:00", help="local start time, HH:MM")
    p_add.add_argument("--treadmill", action="store_true")
    p_add.add_argument("--rpe", type=float, default=None,
                       help="session effort 1-10; overrides pace when pace understates the work")
    p_add.add_argument("--effort", choices=["easy", "moderate", "hard"], default=None)
    p_add.set_defaults(func=cmd_add_run)

    p_eff = sub.add_parser("set-effort", help="attach a session RPE to a run already logged")
    p_eff.add_argument("--date", required=True, help="YYYY-MM-DD")
    p_eff.add_argument("--rpe", type=float, default=None, help="1-10")
    p_eff.add_argument("--effort", choices=["easy", "moderate", "hard"], default=None)
    p_eff.add_argument("--segment-km", type=float, default=None,
                       help="length of the key interval rep, e.g. 1.6")
    p_eff.add_argument("--segment-time", default=None,
                       help="time for the fastest rep of that length, mm:ss")
    p_eff.set_defaults(func=cmd_set_effort)

    p_dash = sub.add_parser("dashboard", help="rebuild dashboard.html")
    p_dash.add_argument("--no-open", action="store_true")
    p_dash.add_argument("--web", action="store_true",
                        help="also write dashboard-web.html for hosting")
    p_dash.set_defaults(func=cmd_dashboard)

    sub.add_parser("races", help="show activities that look like races").set_defaults(func=cmd_races)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
