"""Strava OAuth2: one interactive authorisation, then silent refreshes forever."""

from __future__ import annotations

import http.server
import json
import threading
import time
import urllib.parse
import webbrowser

import requests

from .config import TOKEN_PATH, credentials, ensure_dirs

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
# activity:read_all is what makes private activities visible too.
SCOPE = "read,activity:read_all,profile:read_all"
PORT = 8721
REDIRECT_URI = f"http://localhost:{PORT}/exchange_token"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    granted_scope: str = ""

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            _CallbackHandler.granted_scope = params.get("scope", [""])[0]
            body = (
                b"<html><body style='font-family:system-ui;padding:3rem'>"
                b"<h1>Strava connected</h1><p>Close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
        elif "error" in params:
            _CallbackHandler.error = params["error"][0]
            body = b"<html><body style='font-family:system-ui;padding:3rem'><h1>Authorisation denied</h1></body></html>"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the console clean
        pass


def authorize_url(client_id: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "approval_prompt": "force",
            "scope": SCOPE,
        }
    )
    return f"{AUTH_URL}?{query}"


def interactive_authorize(timeout: int = 300) -> dict:
    """Open the consent page, catch the redirect locally, swap code for tokens."""
    client_id, client_secret = credentials()
    url = authorize_url(client_id)

    server = http.server.HTTPServer(("127.0.0.1", PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("Opening Strava authorisation in your browser.")
    print("If nothing opens, paste this URL yourself:\n")
    print(url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _CallbackHandler.code or _CallbackHandler.error:
            break
        time.sleep(0.3)
    server.shutdown()

    if _CallbackHandler.error:
        raise SystemExit(f"Strava returned an error: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise SystemExit("Timed out waiting for the Strava redirect.")
    if "activity:read_all" not in _CallbackHandler.granted_scope:
        print(
            "Warning: activity:read_all was not granted - private activities will be missing.\n"
            "Re-run `auth` and tick every box on the consent screen."
        )

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": _CallbackHandler.code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    response.raise_for_status()
    tokens = response.json()
    _save(tokens)
    athlete = tokens.get("athlete", {})
    name = " ".join(filter(None, [athlete.get("firstname"), athlete.get("lastname")]))
    print(f"Authorised as {name or 'athlete'} (id {athlete.get('id')}).")
    return tokens


def _save(tokens: dict) -> None:
    ensure_dirs()
    keep = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": tokens["expires_at"],
        "athlete_id": tokens.get("athlete", {}).get("id"),
    }
    existing = _load_raw()
    if keep["athlete_id"] is None and existing:
        keep["athlete_id"] = existing.get("athlete_id")
    TOKEN_PATH.write_text(json.dumps(keep, indent=2) + "\n")
    TOKEN_PATH.chmod(0o600)


def _load_raw() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    return json.loads(TOKEN_PATH.read_text())


def access_token() -> str:
    """Return a valid access token, refreshing it if it is close to expiry."""
    tokens = _load_raw()
    if not tokens:
        raise SystemExit("Not authorised yet. Run:  ./run auth")

    if tokens["expires_at"] - time.time() > 300:
        return tokens["access_token"]

    client_id, client_secret = credentials()
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code == 400:
        raise SystemExit("Refresh token rejected by Strava. Run `./run auth` again.")
    response.raise_for_status()
    fresh = response.json()
    fresh.setdefault("athlete", {"id": tokens.get("athlete_id")})
    _save(fresh)
    return fresh["access_token"]


def athlete_id() -> int | None:
    tokens = _load_raw()
    return tokens.get("athlete_id") if tokens else None
