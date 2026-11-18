"""Paths, .env loading and the athlete profile (HR zones, races)."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "strava.db"
TOKEN_PATH = DATA_DIR / "tokens.json"
ENV_PATH = ROOT / ".env"
PROFILE_PATH = ROOT / "profile.json"
DASHBOARD_PATH = ROOT / "dashboard.html"

DEFAULT_PROFILE = {
    "_comment": "Physiology and race calendar. Anything left null is estimated from your data.",
    "max_hr": None,
    "resting_hr": None,
    "threshold_hr": None,
    "threshold_pace_s_per_km": None,
    "build_weeks": 16,
    "races": [],
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """Minimal .env reader so we don't need python-dotenv."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def credentials() -> tuple[str, str]:
    load_env()
    client_id = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing Strava credentials.\n"
            f"Copy .env.example to .env and fill in the two values from\n"
            "https://www.strava.com/settings/api"
        )
    return client_id, client_secret


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        PROFILE_PATH.write_text(json.dumps(DEFAULT_PROFILE, indent=2) + "\n")
        return dict(DEFAULT_PROFILE)
    profile = json.loads(PROFILE_PATH.read_text())
    merged = dict(DEFAULT_PROFILE)
    merged.update(profile)
    return merged


def save_profile(profile: dict) -> None:
    PROFILE_PATH.write_text(json.dumps(profile, indent=2) + "\n")
