"""Rate-limit-aware Strava API client.

Strava allows 100 requests per 15 minutes and 1000 per day (read quota is the
binding one for us). Short-window exhaustion is handled by sleeping until the
next quarter-hour; daily exhaustion raises so a sync can stop cleanly and be
resumed tomorrow.
"""

from __future__ import annotations

import time

import requests

from .auth import access_token

BASE = "https://www.strava.com/api/v3"


class DailyLimitReached(RuntimeError):
    """Raised when the 24h read quota is spent."""


class StravaClient:
    def __init__(self, verbose: bool = True) -> None:
        self.session = requests.Session()
        self.verbose = verbose
        self.short_usage = 0
        self.short_limit = 100
        self.daily_usage = 0
        self.daily_limit = 1000
        self.requests_made = 0

    # -- rate limit bookkeeping -------------------------------------------------

    def _read_limits(self, response: requests.Response) -> None:
        # Strava sends both an overall and a read-specific quota; take the tighter.
        for limit_header, usage_header in (
            ("X-RateLimit-Limit", "X-RateLimit-Usage"),
            ("X-ReadRateLimit-Limit", "X-ReadRateLimit-Usage"),
        ):
            limit = response.headers.get(limit_header)
            usage = response.headers.get(usage_header)
            if not limit or not usage:
                continue
            try:
                short_limit, daily_limit = (int(x) for x in limit.split(",")[:2])
                short_usage, daily_usage = (int(x) for x in usage.split(",")[:2])
            except ValueError:
                continue
            self.short_limit = min(self.short_limit, short_limit)
            self.daily_limit = min(self.daily_limit, daily_limit)
            self.short_usage = max(self.short_usage, short_usage)
            self.daily_usage = max(self.daily_usage, daily_usage)

    @staticmethod
    def _seconds_to_next_window() -> float:
        now = time.time()
        return (900 - (now % 900)) + 5

    def _guard(self) -> None:
        if self.daily_usage >= self.daily_limit - 2:
            raise DailyLimitReached(
                f"Daily read quota spent ({self.daily_usage}/{self.daily_limit}). "
                "Re-run the sync tomorrow - it resumes where it stopped."
            )
        if self.short_usage >= self.short_limit - 1:
            wait = self._seconds_to_next_window()
            if self.verbose:
                print(f"  rate limit: sleeping {wait:.0f}s for the next 15-minute window")
            time.sleep(wait)
            self.short_usage = 0

    # -- requests ---------------------------------------------------------------

    def get(self, path: str, params: dict | None = None, retries: int = 3):
        self._guard()
        url = f"{BASE}{path}"
        for attempt in range(retries):
            headers = {"Authorization": f"Bearer {access_token()}"}
            response = self.session.get(url, headers=headers, params=params, timeout=60)
            self._read_limits(response)
            self.requests_made += 1

            if response.status_code == 429:
                wait = self._seconds_to_next_window()
                if self.daily_usage >= self.daily_limit - 2:
                    raise DailyLimitReached("Strava returned 429 on the daily quota.")
                if self.verbose:
                    print(f"  429 from Strava: sleeping {wait:.0f}s")
                time.sleep(wait)
                self.short_usage = 0
                continue
            if response.status_code == 404:
                return None
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Giving up on {path} after {retries} attempts")

    # -- endpoints --------------------------------------------------------------

    def activities_page(self, page: int, per_page: int = 200, after: int | None = None):
        params = {"page": page, "per_page": per_page}
        if after:
            params["after"] = after
        return self.get("/athlete/activities", params) or []

    def activity_detail(self, activity_id: int):
        return self.get(f"/activities/{activity_id}", {"include_all_efforts": "true"})

    def activity_streams(self, activity_id: int):
        keys = "time,distance,heartrate,altitude,velocity_smooth,cadence,moving,grade_smooth"
        return self.get(
            f"/activities/{activity_id}/streams",
            {"keys": keys, "key_by_type": "true"},
        )

    def athlete(self):
        return self.get("/athlete")

    def athlete_zones(self):
        return self.get("/athlete/zones")
