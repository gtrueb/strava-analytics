"""Training analysis: build-up windows, intensity mix, fitness curves, race prediction.

Everything here is pure computation over the local SQLite database and returns
plain Python structures, so the dashboard is only a rendering layer.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta

import pandas as pd

from . import db
from .config import load_profile

RUN_TYPES = ("Run", "TrailRun", "VirtualRun")
HM_KM = 21.0975
PLAN_MARKER_RE = re.compile(
    r"(woche|week)\s*\d+\s*/\s*\d+|half.?marathon plan|marathon plan|training plan",
    re.IGNORECASE,
)
RACE_NAME_RE = re.compile(
    r"halbmarathon|half.?marathon|\bhm\b|21[.,]1|21k|\brace\b|wettkampf|rennen|lauf\b",
    re.IGNORECASE,
)

# Session RPE -> intensity factor, for runs where pace does not reflect effort.
# A curved manual treadmill (Assault Runner) costs 20-30% more oxygen at the same
# speed, so its pace understates the work; the same applies to heat, sand or snow.
RPE_INTENSITY = {
    1: 0.55, 2: 0.62, 3: 0.70, 4: 0.76, 5: 0.82,
    6: 0.87, 7: 0.92, 8: 0.97, 9: 1.02, 10: 1.08,
}


def rpe_band(rpe: float) -> str:
    return "easy" if rpe <= 4 else ("moderate" if rpe <= 7 else "hard")


# Distances Strava reports as best efforts, in metres.
EFFORT_DISTANCES = {
    "1k": 1000,
    "5k": 5000,
    "10k": 10000,
    "15k": 15000,
    "Half-Marathon": HM_KM * 1000,
}


# --------------------------------------------------------------------------- io


def load_runs(conn=None) -> pd.DataFrame:
    close = conn is None
    conn = conn or db.connect()
    try:
        placeholders = ",".join("?" * len(RUN_TYPES))
        frame = pd.read_sql_query(
            f"""
            SELECT id, name, sport_type, start_date_local, distance_m, moving_time_s,
                   elapsed_time_s, total_elevation_gain, average_speed, average_heartrate,
                   max_heartrate, has_heartrate, average_cadence, suffer_score, workout_type,
                   trainer, commute, rpe, effort_override, segment_km, segment_time_s
            FROM activities
            WHERE sport_type IN ({placeholders}) AND distance_m > 0 AND moving_time_s > 0
            ORDER BY start_date_local
            """,
            conn,
            params=RUN_TYPES,
        )
    finally:
        if close:
            conn.close()

    if frame.empty:
        return frame

    frame["ts"] = pd.to_datetime(frame["start_date_local"], format="ISO8601", errors="coerce")
    frame = frame.dropna(subset=["ts"])
    frame["date"] = frame["ts"].dt.date
    frame["week_start"] = frame["ts"].dt.to_period("W-SUN").dt.start_time.dt.date
    frame["year"] = frame["ts"].dt.year
    frame["distance_km"] = frame["distance_m"] / 1000.0
    frame["hours"] = frame["moving_time_s"] / 3600.0
    frame["pace_s_per_km"] = frame["moving_time_s"] / frame["distance_km"]
    frame["speed_m_per_min"] = frame["distance_m"] / (frame["moving_time_s"] / 60.0)
    return frame


def load_best_efforts(conn=None) -> pd.DataFrame:
    close = conn is None
    conn = conn or db.connect()
    try:
        frame = pd.read_sql_query(
            "SELECT activity_id, name, distance_m, elapsed_time_s, moving_time_s, start_date_local "
            "FROM best_efforts",
            conn,
        )
    finally:
        if close:
            conn.close()
    if frame.empty:
        return frame
    frame["ts"] = pd.to_datetime(frame["start_date_local"], format="ISO8601", errors="coerce")
    frame = frame.dropna(subset=["ts"])
    frame["date"] = frame["ts"].dt.date
    return frame


# ------------------------------------------------------------------ physiology


def resolve_physiology(runs: pd.DataFrame, profile: dict, conn=None) -> dict:
    """Max HR / threshold HR / threshold pace, from the profile or estimated."""
    out = {"source": {}}

    max_hr = profile.get("max_hr")
    if max_hr:
        out["source"]["max_hr"] = "profile"
    elif not runs.empty and runs["max_heartrate"].notna().any():
        # 99th percentile guards against a single spiky sensor reading.
        max_hr = float(runs["max_heartrate"].quantile(0.99))
        out["source"]["max_hr"] = "estimated from your max HR readings"
    out["max_hr"] = round(max_hr) if max_hr else None

    threshold_hr = profile.get("threshold_hr")
    if threshold_hr:
        out["source"]["threshold_hr"] = "profile"
    else:
        zones = _strava_zones(conn)
        if zones:
            threshold_hr = zones
            out["source"]["threshold_hr"] = "Strava heart rate zones"
        elif not runs.empty and runs["average_heartrate"].notna().any():
            hard = runs[(runs["moving_time_s"] >= 1200) & runs["average_heartrate"].notna()]
            if not hard.empty:
                threshold_hr = float(hard["average_heartrate"].quantile(0.95))
                out["source"]["threshold_hr"] = "estimated from your hardest sustained runs"
        if not threshold_hr and max_hr:
            threshold_hr = 0.90 * max_hr
            out["source"]["threshold_hr"] = "estimated as 90% of max HR"
    out["threshold_hr"] = round(threshold_hr) if threshold_hr else None

    threshold_pace = profile.get("threshold_pace_s_per_km")
    if threshold_pace:
        out["source"]["threshold_pace"] = "profile"
    else:
        efforts = load_best_efforts(conn)
        threshold_pace = _estimate_threshold_pace(efforts, runs)
        out["source"]["threshold_pace"] = "estimated from your best 10k/5k efforts"
    out["threshold_pace_s_per_km"] = round(threshold_pace) if threshold_pace else None
    return out


def _strava_zones(conn=None) -> float | None:
    close = conn is None
    conn = conn or db.connect()
    try:
        raw = db.get_meta(conn, "athlete_zones")
    finally:
        if close:
            conn.close()
    if not raw:
        return None
    try:
        zones = json.loads(raw).get("heart_rate", {}).get("zones", [])
    except Exception:
        return None
    # Strava's 5-zone model: the floor of zone 4 sits at threshold.
    if len(zones) >= 4 and zones[3].get("min", -1) > 0:
        return float(zones[3]["min"])
    return None


def _estimate_threshold_pace(efforts: pd.DataFrame, runs: pd.DataFrame) -> float | None:
    """Threshold pace ~ the pace you could hold for an hour.

    Take the *fastest* of the per-distance estimates rather than the first one
    found. A 10k split taken from inside a half marathon is run at half-marathon
    effort, so trusting it over a genuine 5k would put threshold minutes per
    kilometre too slow - and that in turn misclassifies easy runs as moderate.
    """
    candidates = []
    if not efforts.empty:
        recent_cutoff = pd.Timestamp(date.today() - timedelta(days=730))
        for name, factor in (("5k", 1.06), ("10k", 1.02), ("15k", 0.99)):
            subset = efforts[(efforts["name"] == name) & (efforts["ts"] >= recent_cutoff)]
            if subset.empty:
                continue
            best = float(subset["elapsed_time_s"].min())
            candidates.append((best / (EFFORT_DISTANCES[name] / 1000.0)) * factor)
    if candidates:
        return min(candidates)
    if not runs.empty:
        fast = runs[runs["distance_km"] >= 5]
        if not fast.empty:
            return float(fast["pace_s_per_km"].quantile(0.05)) * 1.05
    return None


def rolling_threshold(runs: pd.DataFrame, efforts: pd.DataFrame, fallback: float | None,
                      window_days: int = 365) -> pd.Series:
    """Threshold pace as it stood at the time of each run.

    Judging a 2023 run against a 2026 threshold makes an honest easy run look
    moderate simply because the athlete got faster since. Intensity is relative to
    the fitness of the day, so the reference has to move with it.
    """
    if runs.empty:
        return pd.Series(dtype="float64")
    if efforts.empty:
        return pd.Series(fallback, index=runs.index, dtype="float64")

    short = efforts[efforts["name"].isin(("5k", "10k"))].copy()
    if short.empty:
        return pd.Series(fallback, index=runs.index, dtype="float64")
    short["pace"] = short.apply(
        lambda r: (r["elapsed_time_s"] / (EFFORT_DISTANCES[r["name"]] / 1000.0))
        * (1.06 if r["name"] == "5k" else 1.02),
        axis=1,
    )

    # Centred, not backward-looking. A backward window leaves the first months of
    # the history calibrated on almost no efforts, which yields an absurdly slow
    # threshold and marks genuinely easy running as hard.
    half = timedelta(days=window_days // 2)
    values = []
    for run_date in runs["date"]:
        window = short[
            (short["date"] >= run_date - half) & (short["date"] <= run_date + half)
        ]
        if window.empty:
            window = short
        values.append(float(window["pace"].min()) if not window.empty else fallback)
    return pd.Series(values, index=runs.index, dtype="float64")


def calibrate_riegel(races: list[dict], efforts: pd.DataFrame, lookback_weeks: int = 14) -> dict:
    """Fit the Riegel exponent to this athlete instead of assuming the textbook 1.06.

    The exponent encodes how well short-distance speed carries to long distance.
    Fitting it to past races is far more honest than a generic constant.
    """
    if efforts.empty:
        return {"exponent": 1.06, "source": "textbook default (no efforts on file)", "points": []}

    points = []
    for race in races:
        if race.get("planned") or not race.get("time_s"):
            continue
        race_date = pd.Timestamp(race["date"])
        window_start = race_date - pd.Timedelta(weeks=lookback_weeks)
        for name in ("5k", "10k"):
            subset = efforts[
                (efforts["name"] == name)
                & (efforts["ts"] >= window_start)
                & (efforts["ts"] < race_date)  # exclude splits taken from the race itself
            ]
            if subset.empty:
                continue
            best = float(subset["elapsed_time_s"].min())
            from_km = EFFORT_DISTANCES[name] / 1000.0
            exponent = math.log(race["time_s"] / best) / math.log(HM_KM / from_km)
            # An exponent below 1.0 would mean speed improves with distance. In
            # practice it means the "best" short effort was not a real effort -
            # in a block of easy running the fastest 5k split is just a jog, and
            # calibrating on it would flatter the projection badly.
            usable = 1.00 <= exponent <= 1.25
            points.append(
                {
                    "race": race["date"],
                    "race_time_s": race["time_s"],
                    "from": name,
                    "from_time_s": int(best),
                    "exponent": round(exponent, 4),
                    "used": usable,
                    "note": None if usable else "no hard short effort in this block - not a real test",
                }
            )
            break

    used = [p for p in points if p["used"]]
    if not used:
        return {
            "exponent": 1.06,
            "source": "textbook default (no race had a hard short effort to calibrate against)",
            "points": points,
        }

    exponent = sum(p["exponent"] for p in used) / len(used)
    return {
        "exponent": round(exponent, 4),
        "source": f"fitted to {len(used)} of your own races",
        "points": points,
    }


def current_best_effort(efforts: pd.DataFrame, races: list[dict], weeks: int = 26) -> dict | None:
    """Best recent 5k, ignoring splits recorded during a race."""
    if efforts.empty:
        return None
    race_dates = {r["date"] for r in races}
    cutoff = pd.Timestamp(date.today() - timedelta(weeks=weeks))
    for name in ("5k", "10k"):
        subset = efforts[(efforts["name"] == name) & (efforts["ts"] >= cutoff)]
        subset = subset[~subset["date"].astype(str).isin(race_dates)]
        if subset.empty:
            continue
        row = subset.loc[subset["elapsed_time_s"].idxmin()]
        return {
            "name": name,
            "time_s": int(row["elapsed_time_s"]),
            "date": str(row["date"]),
            "km": EFFORT_DISTANCES[name] / 1000.0,
        }
    return None


# ------------------------------------------------------------------- intensity


def classify_intensity(runs: pd.DataFrame, physiology: dict) -> pd.Series:
    """Three-zone model: easy (below LT1), moderate (LT1-LT2), hard (above LT2)."""
    threshold_hr = physiology.get("threshold_hr")
    threshold_pace = physiology.get("threshold_pace_s_per_km")

    labels = pd.Series("unknown", index=runs.index, dtype="object")

    if threshold_hr:
        ratio = runs["average_heartrate"] / threshold_hr
        has_hr = runs["average_heartrate"].notna()
        labels[has_hr & (ratio < 0.88)] = "easy"
        labels[has_hr & (ratio >= 0.88) & (ratio < 0.99)] = "moderate"
        labels[has_hr & (ratio >= 0.99)] = "hard"

    reference = runs["threshold_pace_at_time"] if "threshold_pace_at_time" in runs.columns else threshold_pace
    if reference is not None:
        # Fall back to pace where HR is missing.
        missing = labels == "unknown"
        ratio = reference / runs["pace_s_per_km"]
        labels[missing & (ratio < 0.83)] = "easy"
        labels[missing & (ratio >= 0.83) & (ratio < 0.97)] = "moderate"
        labels[missing & (ratio >= 0.97)] = "hard"

    # A recorded effort always wins: it is the only honest signal when the pace
    # was produced on equipment or in conditions the pace model cannot see.
    if "effort_override" in runs.columns:
        stated = runs["effort_override"].notna()
        labels[stated] = runs.loc[stated, "effort_override"]
    if "rpe" in runs.columns:
        from_rpe = runs["rpe"].notna() & runs.get(
            "effort_override", pd.Series(index=runs.index, dtype="object")
        ).isna()
        labels[from_rpe] = runs.loc[from_rpe, "rpe"].map(rpe_band)

    return labels


def training_stress(runs: pd.DataFrame, physiology: dict) -> pd.Series:
    """TSS-style load per run: hours * intensity^2 * 100, HR-based where possible."""
    threshold_hr = physiology.get("threshold_hr")
    threshold_pace = physiology.get("threshold_pace_s_per_km")

    intensity = pd.Series(float("nan"), index=runs.index, dtype="float64")
    if threshold_hr:
        intensity = runs["average_heartrate"] / threshold_hr
    if threshold_pace:
        pace_intensity = threshold_pace / runs["pace_s_per_km"]
        intensity = intensity.fillna(pace_intensity)

    # Reported effort overrides both HR and pace for the same reason as above.
    if "rpe" in runs.columns:
        from_rpe = runs["rpe"].map(
            lambda r: RPE_INTENSITY.get(int(r)) if pd.notna(r) else None
        )
        intensity = from_rpe.combine_first(intensity)

    intensity = intensity.clip(upper=1.15).fillna(0.70)
    return runs["hours"] * intensity.pow(2) * 100.0


# ------------------------------------------------------------------ aggregates


def weekly(runs: pd.DataFrame) -> pd.DataFrame:
    if runs.empty:
        return pd.DataFrame()
    grouped = runs.groupby("week_start")
    frame = pd.DataFrame(
        {
            "km": grouped["distance_km"].sum().round(2),
            "hours": grouped["hours"].sum().round(3),
            "runs": grouped["id"].count(),
            "elev_m": grouped["total_elevation_gain"].sum().round(0),
            "long_run_km": grouped["distance_km"].max().round(2),
            "avg_hr": grouped["average_heartrate"].mean().round(1),
            "load": grouped["tss"].sum().round(1),
        }
    )
    for label in ("easy", "moderate", "hard"):
        subset = runs[runs["intensity"] == label].groupby("week_start")["distance_km"].sum()
        frame[f"{label}_km"] = subset.reindex(frame.index).fillna(0).round(2)
    return frame.reset_index()


def fitness_curve(runs: pd.DataFrame) -> pd.DataFrame:
    """Banister-style CTL (42d) / ATL (7d) / TSB from daily training stress."""
    if runs.empty:
        return pd.DataFrame()
    daily = runs.groupby("date")["tss"].sum()
    index = pd.date_range(min(daily.index), date.today(), freq="D")
    daily = daily.reindex(index.date, fill_value=0.0)
    frame = pd.DataFrame({"date": index.date, "tss": daily.to_numpy()})

    # Start both averages from zero. pandas seeds an adjust=False EWMA with the
    # first observation, which would put fitness equal to day one's single session
    # - i.e. claim six weeks of that load were already banked before the data
    # begins. Prepending a zero day makes the recursion start from nothing.
    seeded = pd.concat([pd.Series([0.0]), frame["tss"]], ignore_index=True)
    frame["ctl"] = seeded.ewm(alpha=1 / 42, adjust=False).mean().iloc[1:].to_numpy().round(1)
    frame["atl"] = seeded.ewm(alpha=1 / 7, adjust=False).mean().iloc[1:].to_numpy().round(1)
    frame["tsb"] = (frame["ctl"].shift(1) - frame["atl"].shift(1)).round(1)
    return frame


def efficiency_trend(runs: pd.DataFrame) -> pd.DataFrame:
    """Speed per heartbeat on aerobic runs - rises as aerobic fitness improves."""
    aerobic = runs[
        (runs["moving_time_s"] >= 1500)
        & runs["average_heartrate"].notna()
        & (runs["intensity"].isin(["easy", "moderate"]))
    ].copy()
    if aerobic.empty:
        return pd.DataFrame()
    aerobic["ef"] = (aerobic["speed_m_per_min"] / aerobic["average_heartrate"]).round(4)
    aerobic = aerobic[["date", "ef", "distance_km", "average_heartrate", "pace_s_per_km"]]
    return aerobic.sort_values("date")


# ----------------------------------------------------------------------- races


def list_candidate_races(conn=None) -> list[dict]:
    runs = load_runs(conn)
    if runs.empty:
        return []
    candidates = runs[
        (runs["workout_type"] == 1)
        | (runs["distance_km"] >= 20.0)
        | runs["name"].fillna("").str.contains(RACE_NAME_RE)
    ]
    out = []
    for _, row in candidates.sort_values("ts").iterrows():
        out.append(
            {
                "id": int(row["id"]),
                "date": str(row["date"]),
                "name": row["name"],
                "distance_km": round(float(row["distance_km"]), 2),
                "time_s": int(row["moving_time_s"]),
                "time": fmt_duration(row["moving_time_s"]),
                "is_race_flag": bool(row["workout_type"] == 1),
            }
        )
    return out


def resolve_half_marathons(runs: pd.DataFrame, profile: dict) -> list[dict]:
    """Pick one half marathon per year: profile entries win, otherwise auto-detect."""
    if profile.get("races"):
        races = []
        for entry in profile["races"]:
            race = {
                "date": entry["date"],
                "name": entry.get("name", "Race"),
                "distance_km": entry.get("distance_km", HM_KM),
                "time_s": parse_duration(entry.get("time")),
                "planned": entry.get("planned", False),
            }
            # Link the pinned race to its Strava activity so the race itself can be
            # excluded from build volume, and so we keep its average heart rate.
            if not runs.empty and not race["planned"]:
                same_day = runs[
                    (runs["date"].astype(str) == entry["date"])
                    & (runs["distance_km"] >= 15.0)
                ]
                if not same_day.empty:
                    row = same_day.loc[same_day["distance_km"].idxmax()]
                    race["activity_id"] = int(row["id"])
                    race["gps_time_s"] = int(row["moving_time_s"])
                    race["gps_distance_km"] = round(float(row["distance_km"]), 2)
                    if not pd.isna(row["average_heartrate"]):
                        race["avg_hr"] = float(row["average_heartrate"])
            races.append(race)
        return sorted(races, key=lambda r: r["date"])

    if runs.empty:
        return []

    hm = runs[(runs["distance_km"] >= 20.0) & (runs["distance_km"] <= 23.0)].copy()
    if hm.empty:
        return []
    hm["is_race"] = (hm["workout_type"] == 1) | hm["name"].fillna("").str.contains(RACE_NAME_RE)

    races = []
    for year, group in hm.groupby("year"):
        flagged = group[group["is_race"]]
        pool = flagged if not flagged.empty else group
        # The genuine race is almost always the fastest 21k of the year.
        row = pool.loc[pool["pace_s_per_km"].idxmin()]
        races.append(
            {
                "date": str(row["date"]),
                "name": row["name"],
                "distance_km": round(float(row["distance_km"]), 2),
                "time_s": int(row["moving_time_s"]),
                "avg_hr": None if pd.isna(row["average_heartrate"]) else float(row["average_heartrate"]),
                "activity_id": int(row["id"]),
                "planned": False,
            }
        )
    return sorted(races, key=lambda r: r["date"])


def build_window(runs: pd.DataFrame, race_date: date, weeks: int) -> pd.DataFrame:
    """Runs in the N weeks leading into a race, labelled by weeks-to-go."""
    race_week = race_date - timedelta(days=race_date.weekday())  # weeks start Monday
    start_week = race_week - timedelta(weeks=weeks - 1)
    window = runs[(runs["week_start"] >= start_week) & (runs["week_start"] <= race_week)].copy()
    if window.empty:
        return window
    window["weeks_out"] = window["week_start"].map(lambda w: (race_week - w).days // 7)
    return window


def build_summary(runs: pd.DataFrame, race: dict, weeks: int, exclude_race: bool = True) -> dict:
    race_date = date.fromisoformat(race["date"])
    window = build_window(runs, race_date, weeks)
    if window.empty:
        return {"race": race, "weeks": [], "totals": {}}

    if exclude_race and race.get("activity_id"):
        window = window[window["id"] != race["activity_id"]]

    per_week = []
    for weeks_out in range(weeks - 1, -1, -1):
        chunk = window[window["weeks_out"] == weeks_out]
        per_week.append(
            {
                "weeks_out": weeks_out,
                "km": round(float(chunk["distance_km"].sum()), 1),
                "hours": round(float(chunk["hours"].sum()), 2),
                "runs": int(len(chunk)),
                "long_run_km": round(float(chunk["distance_km"].max()), 1) if len(chunk) else 0.0,
                "elev_m": round(float(chunk["total_elevation_gain"].sum() or 0)),
                "load": round(float(chunk["tss"].sum()), 1),
                "easy_km": round(float(chunk["easy_km"].sum()), 1),
                "moderate_km": round(float(chunk["moderate_km"].sum()), 1),
                "hard_km": round(float(chunk["hard_km"].sum()), 1),
                "avg_hr": round(float(chunk["average_heartrate"].mean()), 1)
                if chunk["average_heartrate"].notna().any()
                else None,
            }
        )

    total_km = float(window["distance_km"].sum())
    easy_km = float(window["easy_km"].sum())
    classified = float(window[["easy_km", "moderate_km", "hard_km"]].sum().sum())
    # Seiler's 80/20 counts sessions, not kilometres, so report both.
    easy_sessions = int((window["intensity"] == "easy").sum())
    weeks_with_runs = int(window["weeks_out"].nunique())

    totals = {
        "km": round(total_km, 1),
        "hours": round(float(window["hours"].sum()), 1),
        "runs": int(len(window)),
        "weeks_with_runs": weeks_with_runs,
        "avg_week_km": round(total_km / weeks, 1),
        "peak_week_km": round(max((w["km"] for w in per_week), default=0), 1),
        "longest_run_km": round(float(window["distance_km"].max()), 1),
        "elev_m": round(float(window["total_elevation_gain"].sum() or 0)),
        "easy_share": round(100 * easy_km / classified, 1) if classified else None,
        "easy_session_share": round(100 * easy_sessions / len(window), 1) if len(window) else None,
        "avg_hr": round(float(window["average_heartrate"].mean()), 1)
        if window["average_heartrate"].notna().any()
        else None,
        "runs_per_week": round(len(window) / weeks, 1),
    }
    return {"race": race, "weeks": per_week, "totals": totals}


# ------------------------------------------------------------------ prediction


def riegel(time_s: float, from_km: float, to_km: float, exponent: float = 1.06) -> float:
    return time_s * (to_km / from_km) ** exponent


def project_with_plan(
    runs: pd.DataFrame,
    efforts: pd.DataFrame,
    races: list[dict],
    target: dict | None,
    weeks: int,
    physiology: dict | None = None,
) -> dict | None:
    """What finishing the imported plan is worth, using this athlete's own exponent.

    Two levers move the prediction: the 5k (speed) and the Riegel exponent
    (how well that speed holds for 21 km). Volume mostly moves the second one,
    so the plan's benefit is modelled there rather than as a flat time bonus.
    """
    from .plan import load_plan

    plan = load_plan()
    if not target:
        return None

    calibration = calibrate_riegel(races, efforts)
    baseline = current_best_effort(efforts, races)
    if not baseline:
        return None

    race_date = date.fromisoformat(target["date"])
    # A session you have already run today is done, not still to come - counting
    # it in both places would inflate the block total by that session.
    run_dates = set(runs["date"].astype(str)) if not runs.empty else set()
    upcoming = [
        s for s in plan
        if s["kind"] == "run" and s["distance_km"] and date.fromisoformat(s["date"]) >= date.today()
        and date.fromisoformat(s["date"]) <= race_date
        and s["date"] not in run_dates
    ]
    planned_km = sum(s["distance_km"] for s in upcoming)

    # Weekly planned volume, keyed the same way as the build charts.
    race_week = race_date - timedelta(days=(race_date.weekday() + 1) % 7)
    per_week: dict[int, float] = {}
    long_run: dict[int, float] = {}
    planned_quality = planned_easy = 0.0
    # Split the prescribed kilometres into the same three bands the build charts
    # use, so the plan can be shown as a projected row in the intensity mix.
    threshold_pace = (physiology or {}).get("threshold_pace_s_per_km")
    bands = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
    for session in upcoming:
        for seg in json.loads(session.get("segments") or "[]"):
            if seg["easy"] or not seg.get("pace_s") or not threshold_pace:
                bands["easy"] += seg["km"]
                continue
            ratio = threshold_pace / seg["pace_s"]
            band = "easy" if ratio < 0.83 else ("moderate" if ratio < 0.97 else "hard")
            bands[band] += seg["km"]

    # The dashed line shows the plan as written, over its whole extent - including
    # weeks already run. Drawing only the remainder made it start mid-chart and
    # read as a break in the data rather than as the plan.
    scheduled = [
        s for s in plan
        if s["kind"] == "run" and s["distance_km"]
        and date.fromisoformat(s["date"]) <= race_date
    ]
    for session in scheduled:
        session_date = date.fromisoformat(session["date"])
        week_start = session_date - timedelta(days=(session_date.weekday() + 1) % 7)
        weeks_out = (race_week - week_start).days // 7
        if 0 <= weeks_out < weeks:
            per_week[weeks_out] = per_week.get(weeks_out, 0.0) + session["distance_km"]
            long_run[weeks_out] = max(long_run.get(weeks_out, 0.0), session["distance_km"])
    # Only what is still to come - the cumulative chart is a running total, so
    # counting weeks already run would add them twice.
    remaining_per_week: dict[int, float] = {}
    for session in upcoming:
        planned_quality += session.get("quality_km") or 0.0
        planned_easy += session.get("easy_km") or 0.0
        session_date = date.fromisoformat(session["date"])
        week_start = session_date - timedelta(days=(session_date.weekday() + 1) % 7)
        weeks_out = (race_week - week_start).days // 7
        if 0 <= weeks_out < weeks:
            remaining_per_week[weeks_out] = (
                remaining_per_week.get(weeks_out, 0.0) + session["distance_km"]
            )

    window = build_window(runs, race_date, weeks)

    done_km = float(window["distance_km"].sum()) if not window.empty else 0.0
    block_km = done_km + planned_km
    longest_planned = max((s["distance_km"] for s in upcoming), default=0.0)
    longest_done = float(window["distance_km"].max()) if not window.empty else 0.0

    exponent = calibration["exponent"]
    base_seconds = baseline["time_s"]
    ratio = HM_KM / baseline["km"]

    def finish(exp: float, speed_gain: float = 0.0) -> int:
        return int(round(base_seconds * (1 - speed_gain) * ratio ** exp))

    # Keep the modelled gain modest: the plan's longest run (16 km) does not beat
    # the best previous block (16.3 km) and frequency stays at two runs a week, so
    # the endurance exponent should shift a little, not transform.
    scenarios = {
        "hold": finish(exponent),
        "plan_completed": finish(exponent - 0.010, 0.015),
        "optimistic": finish(exponent - 0.020, 0.030),
    }

    best_previous = min(
        (r["time_s"] for r in races if r.get("time_s") and not r.get("planned")), default=None
    )
    return {
        "calibration": calibration,
        "baseline": baseline,
        "planned_km": round(planned_km, 1),
        "done_km": round(done_km, 1),
        "block_km": round(block_km, 1),
        "planned_weeks": [
            {
                "weeks_out": k,
                "km": round(v, 1),
                "long_run_km": round(long_run.get(k, 0.0), 1),
            }
            for k, v in sorted(per_week.items(), reverse=True)
        ],
        "remaining_weeks": [
            {"weeks_out": k, "km": round(v, 1)}
            for k, v in sorted(remaining_per_week.items(), reverse=True)
        ],
        "planned_quality_km": round(planned_quality, 1),
        "planned_easy_km": round(planned_easy, 1),
        "planned_bands": {k: round(v, 1) for k, v in bands.items()},
        "longest_planned_km": round(longest_planned, 1),
        "longest_so_far_km": round(longest_done, 1),
        "sessions_left": len(upcoming),
        "scenarios": scenarios,
        "best_previous_s": best_previous,
        "pb_delta_s": None if best_previous is None else best_previous - scenarios["plan_completed"],
    }


def prescription_map(conn=None) -> dict[str, list[dict]]:
    """date -> prescribed segments, from the plan calendar or the Strava description."""
    from .plan import load_plan, parse_segments

    close = conn is None
    conn = conn or db.connect()
    try:
        out: dict[str, list[dict]] = {}
        for session in load_plan(conn):
            segments = json.loads(session["segments"] or "[]")
            if segments:
                out[session["date"]] = segments
        for row in conn.execute(
            "SELECT start_date_local, description FROM activities "
            "WHERE description IS NOT NULL AND TRIM(description) <> ''"
        ):
            when = row["start_date_local"][:10]
            if when in out:
                continue
            segments, _ = parse_segments(row["description"])
            if segments and PLAN_MARKER_RE.search(row["description"] or ""):
                out[when] = segments
        return out
    finally:
        if close:
            conn.close()


def intensity_split(runs: pd.DataFrame, physiology: dict, conn=None) -> pd.DataFrame:
    """Distance in each band per run - the time-in-zone method, not session-goal.

    Labelling a whole run by its main purpose and then charging every kilometre to
    that band is neither of the two accepted methods: it counts a 13.5 km run with
    7 km of quality as 13.5 km of quality. Where a prescription exists, split the
    run across its prescribed segments instead; otherwise fall back to one label
    for the run, which is correct for an unstructured run anyway.
    """
    threshold = (physiology or {}).get("threshold_pace_s_per_km")
    prescriptions = prescription_map(conn)

    def band_for(pace: float | None, reference: float | None) -> str:
        reference = reference or threshold
        if not pace or not reference:
            return "easy"
        ratio = reference / pace
        return "easy" if ratio < 0.83 else ("moderate" if ratio < 0.97 else "hard")

    rows = []
    for _, run in runs.iterrows():
        bands = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
        # Match the compliance table's +/-1 day tolerance: sessions get shifted a
        # day in real life, and an exact-date lookup would fall back to labelling
        # the whole run by its hardest part.
        segments = prescriptions.get(str(run["date"]))
        if segments is None:
            for offset in (-1, 1):
                segments = prescriptions.get(str(run["date"] + timedelta(days=offset)))
                if segments:
                    break
        has_quality = segments and any(not s["easy"] and s.get("pace_s") for s in segments)
        planned_km = sum(s["km"] for s in segments) if segments else 0

        if has_quality and planned_km and 0.6 <= run["distance_km"] / planned_km <= 1.6:
            # Scale the prescription onto what was actually covered.
            scale = run["distance_km"] / planned_km
            reference = run.get("threshold_pace_at_time")
            for seg in segments:
                bands[band_for(None if seg["easy"] else seg.get("pace_s"), reference)] += seg["km"] * scale
        else:
            # No structure, or an all-easy prescription: one label for the run, which
            # is where a recorded RPE belongs (it captures terrain and equipment).
            bands[run["intensity"] if run["intensity"] != "unknown" else "easy"] = run["distance_km"]

        rows.append({
            "id": run["id"],
            "easy_km": round(bands["easy"], 3),
            "moderate_km": round(bands["moderate"], 3),
            "hard_km": round(bands["hard"], 3),
        })
    return pd.DataFrame(rows).set_index("id")


def plan_compliance(runs: pd.DataFrame, conn=None) -> list[dict]:
    """For every plan session already due, compare what was prescribed with what was run.

    Quality segments are checked against the *fastest matching stretch* of the
    recording, so "4 km at 5:05/km" is judged on the best continuous 4 km rather
    than on the session average, which would include the warm-up.
    """
    import json as _json

    from .exportfiles import fastest_segment
    from .plan import load_plan

    close = conn is None
    conn = conn or db.connect()
    try:
        from .plan import parse_segments

        today = date.today()
        sessions = [s for s in load_plan(conn) if date.fromisoformat(s["date"]) <= today]
        planned_dates = {s["date"] for s in sessions}

        # Sessions already completed are not in the calendar feed any more, but Runna
        # copies the prescription into the Strava activity description, so the
        # earlier weeks of the block can still be graded.
        for row in conn.execute(
            "SELECT id, start_date_local, name, description FROM activities "
            "WHERE description IS NOT NULL AND TRIM(description) <> ''"
        ):
            when = row["start_date_local"][:10]
            if when in planned_dates:
                continue
            segments, ceiling = parse_segments(row["description"])
            if not segments:
                continue
            # Only treat it as a prescription if it really is one. A note like
            # "Intervall 4x2km mit 3'' pause" parses to a segment but is a diary
            # entry, not a target, and would grade the athlete against nothing.
            marked = PLAN_MARKER_RE.search(row["description"] or "")
            prescribed_km = sum(s["km"] for s in segments)
            has_targets = any(not s["easy"] for s in segments)
            if not marked and not (has_targets and len(segments) >= 2):
                continue
            sessions.append(
                {
                    "date": when,
                    "title": row["name"],
                    "distance_km": round(sum(s["km"] for s in segments), 2),
                    "kind": "run",
                    "segments": json.dumps(segments),
                    "quality_km": round(sum(s["km"] for s in segments if not s["easy"]), 2),
                    "easy_km": round(sum(s["km"] for s in segments if s["easy"]), 2),
                    "easy_ceiling_s": ceiling,
                }
            )
            planned_dates.add(when)

        if not sessions or runs.empty:
            return []

        sessions.sort(key=lambda s: s["date"])
        rows = []
        for session in sessions:
            session_date = date.fromisoformat(session["date"])
            if session_date > today:
                continue

            # Allow a day either side - sessions get shifted in real life.
            nearby = runs[runs["date"].map(lambda d: abs((d - session_date).days) <= 1)]
            row = {
                "date": session["date"],
                "title": re.sub(r"^\U0001f3c3\s*", "", session["title"]),
                "planned_km": session["distance_km"],
                "kind": session["kind"],
                "quality_km": session["quality_km"],
                "easy_ceiling_s": session["easy_ceiling_s"],
                "status": "missed",
                "actual_km": None,
                "actual_pace_s": None,
                "target_pace_s": None,
                "target_seg_km": None,
                "segment_pace_s": None,
                "delta_s": None,
            }

            if not nearby.empty:
                activity = nearby.loc[nearby["distance_km"].idxmax()]
                planned_km = session["distance_km"] or 0
                # A run on the day that covers barely half the prescription is a
                # different session, not this one - scoring it against a target it
                # was never meant to hit would be noise.
                if planned_km and activity["distance_km"] < 0.6 * planned_km:
                    row["status"] = "not as prescribed"
                    row["actual_km"] = round(float(activity["distance_km"]), 2)
                    row["actual_pace_s"] = round(float(activity["pace_s_per_km"]))
                    rows.append(row)
                    continue
                row["status"] = "done"
                row["actual_km"] = round(float(activity["distance_km"]), 2)
                row["actual_pace_s"] = round(float(activity["pace_s_per_km"]))
                row["activity_date"] = str(activity["date"])

                segments = _json.loads(session["segments"] or "[]")
                quality = [s for s in segments if not s["easy"] and s.get("pace_s")]
                if quality:
                    # Group by rep length, then judge the rep length carrying the
                    # most work. fastest_segment returns the single quickest stretch,
                    # so it must be compared against the *fastest* pace prescribed at
                    # that length - otherwise a session with 5:30 and 5:10 reps looks
                    # wildly overcooked when only the 5:10 rep was run fast.
                    by_length: dict[float, dict] = {}
                    for seg in quality:
                        entry = by_length.setdefault(seg["km"], {"total": 0.0, "pace": seg["pace_s"]})
                        entry["total"] += seg["km"]
                        entry["pace"] = min(entry["pace"], seg["pace_s"])
                    rep_km, info = max(by_length.items(), key=lambda kv: kv[1]["total"])
                    row["target_pace_s"] = info["pace"]
                    row["target_seg_km"] = round(rep_km, 2)

                    seconds = None
                    stream_row = conn.execute(
                        "SELECT data FROM streams WHERE activity_id=?", (int(activity["id"]),)
                    ).fetchone()
                    if stream_row and stream_row["data"]:
                        streams = _json.loads(stream_row["data"])
                        seconds = fastest_segment(streams, rep_km * 1000.0)
                    elif not pd.isna(activity.get("segment_time_s")) and not pd.isna(
                        activity.get("segment_km")
                    ):
                        # Manually logged sessions carry no stream, so use the rep
                        # split the athlete recorded - scaled if the lengths differ.
                        seconds = float(activity["segment_time_s"]) * (
                            rep_km / float(activity["segment_km"])
                        )
                    if seconds:
                        pace = seconds / rep_km
                        row["segment_pace_s"] = round(pace)
                        row["delta_s"] = round(pace - info["pace"])

                if row["delta_s"] is None and row["easy_ceiling_s"] and not quality:
                    row["delta_s"] = row["actual_pace_s"] - row["easy_ceiling_s"]

            rows.append(row)
        return rows
    finally:
        if close:
            conn.close()


def predict_half(efforts: pd.DataFrame, runs: pd.DataFrame, weeks_back: int = 10) -> dict | None:
    """Riegel projection from the best recent effort at any distance."""
    if efforts.empty:
        return None
    cutoff = pd.Timestamp(date.today() - timedelta(weeks=weeks_back))
    recent = efforts[efforts["ts"] >= cutoff]
    if recent.empty:
        cutoff = pd.Timestamp(date.today() - timedelta(weeks=26))
        recent = efforts[efforts["ts"] >= cutoff]
    if recent.empty:
        return None

    # Your best recent performance at *any* distance defines current fitness, so
    # take the fastest projection rather than the longest distance available -
    # a 15k split inside an easy long run would otherwise dominate a 5k race.
    candidates = []
    for name in ("Half-Marathon", "15k", "10k", "5k"):
        subset = recent[recent["name"] == name]
        if subset.empty:
            continue
        row = subset.loc[subset["elapsed_time_s"].idxmin()]
        from_km = EFFORT_DISTANCES[name] / 1000.0
        candidates.append(
            {
                "from": name,
                "from_time_s": int(row["elapsed_time_s"]),
                "from_date": str(row["date"]),
                "predicted_s": int(riegel(float(row["elapsed_time_s"]), from_km, HM_KM)),
                "weeks_back": weeks_back,
            }
        )
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["predicted_s"])


# ----------------------------------------------------------------- formatting


def fmt_duration(seconds) -> str:
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return "-"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fmt_pace(seconds_per_km) -> str:
    if not seconds_per_km or (isinstance(seconds_per_km, float) and math.isnan(seconds_per_km)):
        return "-"
    minutes, secs = divmod(int(round(seconds_per_km)), 60)
    return f"{minutes}:{secs:02d}/km"


def parse_duration(text) -> int | None:
    if not text:
        return None
    parts = [int(p) for p in str(text).split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return int(parts[0])


# -------------------------------------------------------------------- payload


def build_payload(conn=None) -> dict:
    close = conn is None
    conn = conn or db.connect()
    try:
        profile = load_profile()
        runs = load_runs(conn)
        if runs.empty:
            raise SystemExit("No runs in the database yet. Run:  ./run sync")

        physiology = resolve_physiology(runs, profile, conn)
        # One fixed threshold across all years by default. A per-year estimate reads
        # "best 5k that year", which is really a function of whether a hard effort
        # happened to be run - so a year of pure easy running gets a slow threshold
        # and its easy runs get relabelled moderate. That is circular. Set
        # "threshold_follows_fitness": true in profile.json to opt into the rolling
        # reference instead.
        if profile.get("threshold_follows_fitness"):
            runs["threshold_pace_at_time"] = rolling_threshold(
                runs, load_best_efforts(conn), physiology.get("threshold_pace_s_per_km"))
        runs["intensity"] = classify_intensity(runs, physiology)
        runs["tss"] = training_stress(runs, physiology)
        split = intensity_split(runs, physiology, conn)
        runs = runs.join(split, on="id")

        efforts = load_best_efforts(conn)
        weeks = int(profile.get("build_weeks") or 16)
        races = resolve_half_marathons(runs, profile)

        builds = [build_summary(runs, race, weeks) for race in races if not race.get("planned")]

        target = _target_race(races, profile)
        current = build_summary(runs, target, weeks, exclude_race=False) if target else None
        if current:
            today = date.today()
            current["is_current"] = True
            current["weeks_to_go"] = max(0, (date.fromisoformat(target["date"]) - today).days // 7)
            # Count the week in progress everywhere. It is not a finished week, so
            # the dashboard flags it rather than hiding it - a blank current week
            # reads as "did nothing" and understates where the block actually is.
            # Derive the cutoff from the week today actually falls in, using the
            # same indexing as build_window. A raw day count drifts out of step
            # near the weekend and makes the chart draw next week - still empty -
            # as if it were finished, collapsing the line to zero.
            # Must match build_window exactly - weeks start Monday there, and a
            # one-day difference in the race-week anchor shifts everything by a
            # whole week.
            race_week = date.fromisoformat(target["date"])
            race_week -= timedelta(days=race_week.weekday())
            today_week = today - timedelta(days=today.weekday())
            current["completed_cutoff"] = max(0, (race_week - today_week).days // 7)
            current["week_in_progress"] = today.weekday() != 6
            current["week_days_done"] = today.weekday() + 1

        weekly_frame = weekly(runs)
        fitness = fitness_curve(runs)
        ef = efficiency_trend(runs)

        payload = {
            "generated_at": datetime.now().isoformat(timespec="minutes"),
            "today": str(date.today()),
            "build_weeks": weeks,
            "physiology": physiology,
            "totals": _all_time_totals(runs),
            "by_year": _yearly_totals(runs),
            "races": races,
            "builds": builds,
            "current": current,
            "weekly": _records(weekly_frame),
            # Full history: the chart offers a range control back to the first build.
            "fitness": _records(fitness) if not fitness.empty else [],
            "efficiency": _records(ef),
            "best_efforts": _effort_progression(efforts),
            "prediction": predict_half(efforts, runs),
            "projection": project_with_plan(runs, efforts, races, target, weeks, physiology),
            "compliance": plan_compliance(runs, conn),
            "recent_runs": _recent_runs(runs),
            "comparison": _current_comparison(builds, current),
        }
        return payload
    finally:
        if close:
            conn.close()


def _target_race(races: list[dict], profile: dict) -> dict | None:
    """The next race: an explicit planned entry, else the same slot this year."""
    today = date.today()
    upcoming = [r for r in races if date.fromisoformat(r["date"]) >= today]
    if upcoming:
        return dict(upcoming[0], planned=True)

    past = [r for r in races if date.fromisoformat(r["date"]) < today]
    if not past:
        return None
    # Project onto this year using the average day-of-year of previous races.
    days = [date.fromisoformat(r["date"]).timetuple().tm_yday for r in past]
    projected = date(today.year, 1, 1) + timedelta(days=int(sum(days) / len(days)) - 1)
    if projected < today:
        projected = date(today.year + 1, 1, 1) + timedelta(days=int(sum(days) / len(days)) - 1)
    return {
        "date": str(projected),
        "name": f"Projected half marathon {projected.year}",
        "distance_km": HM_KM,
        "time_s": None,
        "planned": True,
        "projected": True,
    }


def _current_comparison(builds: list[dict], current: dict | None, window: int = 4) -> dict | None:
    """Where the current block stands against previous builds at the same weeks-to-go."""
    if not current or not current.get("weeks"):
        return None
    weeks_to_go = current.get("weeks_to_go", 0)
    # The current week counts. It is partial, so the deltas below flatter previous
    # builds slightly - the dashboard labels it rather than dropping the week.
    cutoff = current.get("completed_cutoff", weeks_to_go)

    def window_km(build: dict) -> float | None:
        chunk = [
            w["km"] for w in build["weeks"]
            if cutoff <= w["weeks_out"] < cutoff + window
        ]
        return round(sum(chunk), 1) if chunk else None

    def cumulative_km(build: dict) -> float | None:
        chunk = [w["km"] for w in build["weeks"] if w["weeks_out"] >= cutoff]
        return round(sum(chunk), 1) if chunk else None

    def long_run(build: dict) -> float | None:
        chunk = [w["long_run_km"] for w in build["weeks"] if w["weeks_out"] >= cutoff]
        return round(max(chunk), 1) if chunk else None

    per_build = []
    for build in builds:
        year = build["race"]["date"][:4]
        per_build.append(
            {
                "year": year,
                "window_km": window_km(build),
                "cum_km": cumulative_km(build),
                "long_run_km": long_run(build),
                "race_time_s": build["race"].get("time_s"),
            }
        )

    def mean_of(key: str) -> float | None:
        values = [b[key] for b in per_build if b[key] is not None]
        return sum(values) / len(values) if values else None

    def pct(now, before) -> float | None:
        if now is None or not before:
            return None
        return round(100 * (now - before) / before, 1)

    current_window = window_km(current)
    current_cum = cumulative_km(current)
    current_long = long_run(current)
    prev_window, prev_cum, prev_long = mean_of("window_km"), mean_of("cum_km"), mean_of("long_run_km")

    return {
        "weeks_to_go": weeks_to_go,
        "window_weeks": window,
        "current_km": current_window,
        "prev_avg_km": round(prev_window, 1) if prev_window else None,
        "delta_pct_vs_prev_avg": pct(current_window, prev_window),
        "cum_current": current_cum,
        "cum_prev_avg": round(prev_cum, 1) if prev_cum else None,
        "cum_delta_pct": pct(current_cum, prev_cum),
        "long_run_current": current_long,
        "long_run_prev_avg": round(prev_long, 1) if prev_long else None,
        "long_run_delta_pct": pct(current_long, prev_long),
        "per_build": per_build,
    }


def _all_time_totals(runs: pd.DataFrame) -> dict:
    return {
        "runs": int(len(runs)),
        "km": round(float(runs["distance_km"].sum()), 1),
        "hours": round(float(runs["hours"].sum()), 1),
        "elev_m": round(float(runs["total_elevation_gain"].sum() or 0)),
        "first": str(runs["date"].min()),
        "last": str(runs["date"].max()),
    }


def _yearly_totals(runs: pd.DataFrame) -> list[dict]:
    out = []
    for year, group in runs.groupby("year"):
        out.append(
            {
                "year": int(year),
                "km": round(float(group["distance_km"].sum()), 1),
                "runs": int(len(group)),
                "hours": round(float(group["hours"].sum()), 1),
                "longest_km": round(float(group["distance_km"].max()), 1),
                "avg_hr": round(float(group["average_heartrate"].mean()), 1)
                if group["average_heartrate"].notna().any()
                else None,
            }
        )
    return out


def _effort_progression(efforts: pd.DataFrame) -> dict:
    if efforts.empty:
        return {}
    out = {}
    for name in ("5k", "10k", "15k", "Half-Marathon"):
        subset = efforts[efforts["name"] == name].sort_values("ts")
        if subset.empty:
            continue
        out[name] = [
            {"date": str(row["date"]), "time_s": int(row["elapsed_time_s"])}
            for _, row in subset.iterrows()
        ]
    return out


def _recent_runs(runs: pd.DataFrame, limit: int = 30) -> list[dict]:
    recent = runs.sort_values("ts", ascending=False).head(limit)
    return [
        {
            "date": str(row["date"]),
            "name": row["name"],
            "km": round(float(row["distance_km"]), 2),
            "time_s": int(row["moving_time_s"]),
            "pace_s": round(float(row["pace_s_per_km"])),
            "hr": None if pd.isna(row["average_heartrate"]) else round(float(row["average_heartrate"])),
            "intensity": row["intensity"],
            "load": round(float(row["tss"]), 1),
        }
        for _, row in recent.iterrows()
    ]


def _records(frame: pd.DataFrame) -> list[dict]:
    if frame is None or frame.empty:
        return []
    frame = frame.copy()
    for column in frame.columns:
        if frame[column].dtype == "object":
            frame[column] = frame[column].map(lambda v: str(v) if isinstance(v, date) else v)
    return json.loads(frame.to_json(orient="records", date_format="iso"))
