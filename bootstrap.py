"""One-time Spotify OAuth 2.0 bootstrap helper.

Run this on your local machine (not in Horizon) after registering a
Spotify Developer app. It:

  1. Opens the browser to Spotify's authorization page.
  2. Catches the redirect at http://127.0.0.1:8765/callback.
  3. Exchanges the authorization code for an access + refresh token.
  4. Prints the refresh token for you to add as a Horizon secret.

Usage:

    export SPOTIFY_CLIENT_ID=...       # from Spotify app dashboard
    export SPOTIFY_CLIENT_SECRET=...
    python bootstrap.py

Prerequisite: the Spotify app MUST have this exact redirect URI registered:

    http://127.0.0.1:8765/callback

Scopes requested:
    playlist-modify-private
    playlist-modify-public
    playlist-read-private
    playlist-read-collaborative
    user-read-playback-state
    user-modify-playback-state
    user-read-currently-playing
    user-read-recently-played
    user-library-read
    user-library-modify
    user-top-read
    user-follow-read
    user-follow-modify
"""

import base64
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"
SCOPES = (
    "playlist-modify-private playlist-modify-public "
    "playlist-read-private playlist-read-collaborative "
    "user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing user-read-recently-played "
    "user-library-read user-library-modify "
    "user-top-read user-follow-read user-follow-modify"
)
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


captured: dict = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        captured["code"] = qs.get("code", [None])[0]
        captured["state"] = qs.get("state", [None])[0]
        captured["error"] = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if captured["error"]:
            body = f"<h1>Authorization failed</h1><p>{captured['error']}</p>"
        else:
            body = (
                "<h1>Authorization received</h1>"
                "<p>You can close this tab and return to the terminal.</p>"
            )
        self.wfile.write(body.encode())


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
    ).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in env.", file=sys.stderr)
        sys.exit(1)

    state = secrets.token_urlsafe(16)
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "show_dialog": "true",
        }
    )
    auth_url = f"{AUTH_URL}?{params}"

    server = http.server.HTTPServer(("127.0.0.1", PORT), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("Opening browser to Spotify authorization...")
    print(f"If it does not open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print(f"Waiting for redirect to {REDIRECT_URI} ...")
    while "code" not in captured and "error" not in captured:
        pass
    server.shutdown()

    if captured.get("error"):
        print(f"Authorization failed: {captured['error']}", file=sys.stderr)
        sys.exit(1)
    if captured.get("state") != state:
        print("ERROR: state mismatch; possible CSRF. Aborting.", file=sys.stderr)
        sys.exit(1)

    code = captured["code"]
    print("Got authorization code. Exchanging for tokens...")
    tokens = exchange_code(client_id, client_secret, code)

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("ERROR: no refresh_token in response:", file=sys.stderr)
        print(json.dumps(tokens, indent=2), file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("SUCCESS. Add this as SPOTIFY_REFRESH_TOKEN in Horizon:")
    print("=" * 60)
    print(refresh)
    print("=" * 60)
    print("\nKeep this token private. You do not need the access token.")


if __name__ == "__main__":
    main()
