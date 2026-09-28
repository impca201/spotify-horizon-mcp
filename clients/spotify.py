"""Spotify Web API client with OAuth 2.0 refresh-token flow."""

import asyncio
import base64
import re
import time

import httpx

SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(Exception):
    """Raised when the Spotify API returns an error."""


def _normalize_track(track: dict) -> dict:
    """Convert a raw Spotify track object to the normalized shape shared with
    the Tidal client. Used by get_playlist_tracks (with added_at layered on
    top by the caller) and by the ISRC/fuzzy search helpers."""
    album = track.get("album") or {}
    return {
        "id": track.get("id"),
        "uri": track.get("uri"),
        "name": track.get("name"),
        "artists": [
            {"id": a.get("id"), "name": a.get("name")}
            for a in track.get("artists", []) or []
        ],
        "album": {"id": album.get("id"), "name": album.get("name")},
        "isrc": (track.get("external_ids") or {}).get("isrc"),
        "duration_ms": track.get("duration_ms"),
    }


class SpotifyClient:
    """Async Spotify client. Manages access-token refresh transparently."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._user_id: str | None = None
        self._token_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"User-Agent": "mcp-spotify/1.0"},
        )

    async def close(self):
        await self._client.aclose()

    # ---- token management ----

    def _basic_auth(self) -> str:
        raw = f"{self._client_id}:{self._client_secret}".encode()
        return base64.b64encode(raw).decode()

    @staticmethod
    def _describe_error(resp, context: str) -> str:
        """Describe an upstream failure without pasting its body into a tool result.

        A tool result goes straight into an agent's context and is frequently
        logged and summarised from there, so an upstream body is the wrong
        thing to forward verbatim. Two reasons, and the second is the one that
        makes this worth doing rather than merely tidy.

        Spotify's ERROR BODIES ARE NOT A STABLE CONTRACT. They are JSON on the
        Web API but form-encoded on the token endpoint, and an edge failure
        (a proxy, a maintenance page, a rate-limit interstitial) returns HTML.
        Forwarding an arbitrary blob means the agent reads whatever the edge
        happened to serve, which is how a plain 502 becomes a page of markup in
        the model's context.

        And the TOKEN endpoint is the sharp case. The request carries the
        client secret in a Basic header and the refresh token in the body, and
        an OAuth error response echoes request parameters back. That is the one
        response on this client whose body can plausibly contain credential
        material, and it was the one being pasted in full.

        Status and endpoint are kept, because an error nobody can diagnose is
        its own problem. Spotify's own machine-readable reason is extracted
        when the body really is the documented JSON shape -- a short, known
        field, not an arbitrary blob.
        """
        reason = ""
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is exactly the case
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                reason = str(err.get("message") or "")[:200]
            elif isinstance(err, str):
                # The token endpoint's shape: {"error": "invalid_grant"}. The
                # code is safe; error_description can quote the request, so it
                # is deliberately not read here.
                reason = err[:200]
        return f"{context} failed ({resp.status_code})" + (f": {reason}" if reason else "")

    async def _refresh_access_token(self):
        resp = await self._client.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            headers={"Authorization": f"Basic {self._basic_auth()}"},
        )
        if resp.status_code != 200:
            raise SpotifyError(self._describe_error(resp, "Token refresh"))
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        # Spotify may rotate the refresh token; use the new one if returned
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]

    async def _ensure_token(self):
        if not all((self._client_id, self._client_secret, self._refresh_token)):
            raise SpotifyError(
                "Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and "
                "SPOTIFY_REFRESH_TOKEN in Horizon before calling tools."
            )
        if self._access_token and time.time() < self._expires_at - 60:
            return
        async with self._token_lock:
            # Another task may have refreshed while we waited on the lock.
            # Spotify rotates refresh tokens on use, so concurrent refreshes
            # would invalidate each other.
            if self._access_token and time.time() < self._expires_at - 60:
                return
            await self._refresh_access_token()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        await self._ensure_token()
        url = f"{SPOTIFY_API_BASE}{path}"
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._access_token}"}
            resp = await self._client.request(
                method, url, params=params, json=json_body, headers=headers
            )
            if resp.status_code == 401 and attempt == 0:
                # Token may have been revoked or expired early, force refresh and retry
                self._access_token = None
                await self._ensure_token()
                continue
            if resp.status_code == 429 and attempt == 0:
                # Respect Retry-After (seconds), cap so tool calls don't hang.
                try:
                    delay = float(resp.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(min(max(delay, 0.0), 30.0))
                continue
            if resp.status_code >= 400:
                raise SpotifyError(
                    self._describe_error(resp, f"Spotify API {method} {path}")
                )
            if not resp.content:
                return {}
            return resp.json()
        raise SpotifyError(f"Request failed after retry: {method} {path}")

    # ---- public API ----

    async def search_artists(self, query: str, limit: int = 5) -> list[dict]:
        """Return matching artists. Note: new-dev-mode apps cap search at 10
        results, and `popularity`/`genres`/`followers` may be absent on
        individual items."""
        # Always request >=2 — with limit=1 Spotify sometimes returns a related
        # artist (e.g. "Radiohead" → "Thom Yorke") instead of the exact match.
        requested = max(2, min(limit, 10))
        data = await self._request(
            "GET",
            "/search",
            params={"q": query, "type": "artist", "limit": requested},
        )
        items = data.get("artists", {}).get("items", [])
        results = [
            {
                "id": a["id"],
                "name": a["name"],
                "popularity": a.get("popularity"),
                "genres": a.get("genres", []),
                "followers": a.get("followers", {}).get("total") if isinstance(a.get("followers"), dict) else None,
                "url": a.get("external_urls", {}).get("spotify"),
            }
            for a in items
        ]
        # Prefer an exact (case-insensitive) name match if one exists
        q_lower = query.strip().lower()
        exact = [r for r in results if r["name"].lower() == q_lower]
        if exact:
            other = [r for r in results if r["name"].lower() != q_lower]
            results = exact + other
        return results[: max(1, min(limit, 10))]

    async def get_top_tracks_for_artist(
        self, artist_name: str, limit: int = 5, market: str = "US"
    ) -> list[dict]:
        """Return the artist's most popular tracks available in `market`.

        Works around two Spotify Development Mode restrictions:
          * `/artists/{id}/top-tracks` returns 403 for new dev apps.
          * `/search` caps limit at 10 and strips the `popularity` field.

        Strategy: resolve artist to a canonical ID, then run a track-search with
        `artist:"NAME"` filter, keep only tracks actually credited to that ID,
        and rely on Spotify's relevance ordering (≈ popularity for this query
        shape) because we can no longer see `popularity` directly.
        """
        matches = await self.search_artists(artist_name, limit=5)
        if not matches:
            return []
        artist_id = matches[0]["id"]
        canonical = matches[0]["name"]

        # Strip double quotes from the canonical name: they would close our
        # `artist:"..."` filter and corrupt the query (silently returning no
        # tracks for artists like `"Weird Al" Yankovic`).
        safe_canonical = canonical.replace('"', "")
        data = await self._request(
            "GET",
            "/search",
            params={
                "q": f'artist:"{safe_canonical}"',
                "type": "track",
                "limit": 10,  # dev-mode cap
                "market": market,
            },
        )
        items = data.get("tracks", {}).get("items", [])
        filtered = [
            t for t in items
            if any(a.get("id") == artist_id for a in t.get("artists", []))
        ]
        # De-duplicate by (track name, primary artist) to suppress multiple
        # album / compilation copies of the same recording.
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for t in filtered:
            key = (t["name"].lower(), t["artists"][0]["id"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(t)
        return [
            {
                "id": t["id"],
                "name": t["name"],
                "uri": t["uri"],
                "album": t.get("album", {}).get("name"),
                "artists": [ar["name"] for ar in t.get("artists", [])],
                "url": t.get("external_urls", {}).get("spotify"),
            }
            for t in deduped[: max(1, min(limit, 10))]
        ]

    async def get_current_user_id(self) -> str:
        """Return the authenticated user's Spotify user ID (cached)."""
        if self._user_id:
            return self._user_id
        data = await self._request("GET", "/me")
        self._user_id = data["id"]
        return self._user_id

    async def create_playlist(
        self, name: str, public: bool = False, description: str = ""
    ) -> dict:
        """Create a new playlist on the authenticated user's account.

        Uses POST /me/playlists per the Feb 2026 API migration — the older
        POST /users/{user_id}/playlists was removed.
        """
        body: dict = {"name": name, "public": public}
        if description:
            body["description"] = description
        data = await self._request("POST", "/me/playlists", json_body=body)
        return {
            "id": data["id"],
            "url": data.get("external_urls", {}).get("spotify"),
            "name": data["name"],
        }

    async def add_tracks(self, playlist_id: str, uris: list[str]) -> int:
        """Add tracks to a playlist. Spotify caps at 100 URIs per request.

        Uses POST /playlists/{id}/items per the Feb 2026 migration (renamed
        from /tracks).
        """
        added = 0
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            await self._request(
                "POST", f"/playlists/{playlist_id}/items", json_body={"uris": chunk}
            )
            added += len(chunk)
        return added

    async def remove_tracks(self, playlist_id: str, uris: list[str]) -> int:
        """Remove tracks from a playlist. Chunks at 100 URIs (Spotify limit).

        Post-Feb-2026 DELETE body shape is `{"items": [{"uri": "..."}]}` — the
        old `tracks` key was renamed to `items`, but the object wrapper stays.
        Bare URI strings return 400 "Invalid base62 id"; bare `uris` key
        returns 400 "No uris provided".
        """
        removed = 0
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            await self._request(
                "DELETE",
                f"/playlists/{playlist_id}/items",
                json_body={"items": [{"uri": u} for u in chunk]},
            )
            removed += len(chunk)
        return removed

    async def update_playlist(
        self,
        playlist_id: str,
        name: str | None = None,
        description: str | None = None,
        public: bool | None = None,
    ) -> None:
        """Rename a playlist, change its description, or toggle visibility."""
        body: dict = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if public is not None:
            body["public"] = public
        if not body:
            return
        await self._request("PUT", f"/playlists/{playlist_id}", json_body=body)

    async def unfollow_playlist(self, playlist_id: str) -> None:
        """Spotify's equivalent of "delete a playlist" — removes it from the
        user's library by unfollowing. The playlist object itself persists on
        Spotify's side but drops out of the user's view."""
        await self._request("DELETE", f"/playlists/{playlist_id}/followers")

    async def get_my_playlists(self, limit: int = 50) -> list[dict]:
        """Return the authenticated user's playlists (owned + followed).

        Requires `playlist-read-private` scope.
        """
        results: list[dict] = []
        offset = 0
        page_size = max(1, min(limit, 50))
        while len(results) < limit:
            data = await self._request(
                "GET",
                "/me/playlists",
                params={"limit": min(page_size, limit - len(results)), "offset": offset},
            )
            items = data.get("items", [])
            if not items:
                break
            for p in items:
                owner = p.get("owner", {}) or {}
                results.append(
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "url": p.get("external_urls", {}).get("spotify"),
                        "track_count": p.get("tracks", {}).get("total"),
                        "public": p.get("public"),
                        "owner_id": owner.get("id"),
                        "owner_name": owner.get("display_name"),
                    }
                )
            if not data.get("next"):
                break
            offset += len(items)
        return results

    async def search_track_by_isrc(self, isrc: str, market: str = "US") -> list[dict]:
        """Look up Spotify tracks by ISRC. Returns the normalized track shape.

        Spotify's `/search` endpoint supports an `isrc:` field operator. ISRC
        can resolve to multiple regional variants, so this returns a list and
        the caller picks the right one (the sync matcher prefers the market
        match).
        """
        data = await self._request(
            "GET",
            "/search",
            params={
                "q": f"isrc:{isrc}",
                "type": "track",
                "limit": 10,
                "market": market,
            },
        )
        items = (data.get("tracks") or {}).get("items", []) or []
        return [_normalize_track(t) for t in items]

    async def search_track_fuzzy(
        self, title: str, artist: str, duration_ms: int | None = None
    ) -> list[dict]:
        """Fallback when ISRC isn't available or didn't match on the target side.

        Caller (matcher) is responsible for picking the best result; this
        method just runs the search and returns normalized candidates.
        """
        # Strip double quotes from the strings: they would close our
        # `artist:"..."` / `track:"..."` filters and corrupt the query.
        safe_title = (title or "").replace('"', "")
        safe_artist = (artist or "").replace('"', "")
        if not safe_title or not safe_artist:
            return []
        q = f'track:"{safe_title}" artist:"{safe_artist}"'
        data = await self._request(
            "GET",
            "/search",
            params={"q": q, "type": "track", "limit": 10},
        )
        items = (data.get("tracks") or {}).get("items", []) or []
        return [_normalize_track(t) for t in items]

    async def search_catalog(
        self, query: str, item_type: str = "track", limit: int = 10
    ) -> list[dict]:
        """Search tracks, albums, artists or playlists within Dev Mode's cap."""
        if item_type not in {"track", "album", "artist", "playlist"}:
            raise ValueError("item_type must be track, album, artist or playlist")
        if not query.strip():
            raise ValueError("query cannot be empty")
        data = await self._request(
            "GET", "/search",
            params={"q": query.strip(), "type": item_type, "limit": max(1, min(limit, 10))},
        )
        collection = (data.get(item_type + "s") or {}).get("items") or []
        return [
            {
                "id": item.get("id"),
                "uri": item.get("uri"),
                "name": item.get("name"),
                "type": item_type,
                "artists": [a.get("name") for a in item.get("artists") or []],
                "album": (item.get("album") or {}).get("name"),
                "url": (item.get("external_urls") or {}).get("spotify"),
            }
            for item in collection if item
        ]

    async def get_playback_state(self) -> dict:
        data = await self._request("GET", "/me/player")
        item = data.get("item") or {}
        return {
            "is_playing": data.get("is_playing", False),
            "progress_ms": data.get("progress_ms"),
            "device": data.get("device"),
            "track": _normalize_track(item) if item.get("type") == "track" else None,
            "context": data.get("context"),
        }

    async def get_devices(self) -> list[dict]:
        data = await self._request("GET", "/me/player/devices")
        return data.get("devices") or []

    async def start_playback(
        self, track: str | None = None, device_id: str | None = None
    ) -> None:
        body = {"uris": [self.parse_track_ref(track)]} if track else None
        params = {"device_id": device_id} if device_id else None
        await self._request("PUT", "/me/player/play", params=params, json_body=body)

    async def playback_action(self, action: str) -> None:
        endpoints = {
            "pause": ("PUT", "/me/player/pause"),
            "next": ("POST", "/me/player/next"),
            "previous": ("POST", "/me/player/previous"),
        }
        if action not in endpoints:
            raise ValueError("action must be pause, next or previous")
        method, path = endpoints[action]
        await self._request(method, path)

    async def set_volume(self, volume_percent: int) -> None:
        if not 0 <= volume_percent <= 100:
            raise ValueError("volume_percent must be between 0 and 100")
        await self._request(
            "PUT", "/me/player/volume", params={"volume_percent": volume_percent}
        )

    async def get_queue(self) -> dict:
        data = await self._request("GET", "/me/player/queue")
        return {
            "currently_playing": _normalize_track(data["currently_playing"])
            if (data.get("currently_playing") or {}).get("type") == "track" else None,
            "queue": [
                _normalize_track(item) for item in data.get("queue") or []
                if item and item.get("type") == "track"
            ],
        }

    async def add_to_queue(self, track: str, device_id: str | None = None) -> None:
        params = {"uri": self.parse_track_ref(track)}
        if device_id:
            params["device_id"] = device_id
        await self._request("POST", "/me/player/queue", params=params)

    async def get_saved_tracks(self, limit: int = 20) -> list[dict]:
        data = await self._request(
            "GET", "/me/tracks", params={"limit": max(1, min(limit, 50))}
        )
        return [
            {"added_at": item.get("added_at"), "track": _normalize_track(item["track"])}
            for item in data.get("items") or [] if item.get("track")
        ]

    async def save_track(self, track: str, save: bool = True) -> None:
        track_id = self.parse_track_ref(track).rsplit(":", 1)[-1]
        await self._request(
            "PUT" if save else "DELETE", "/me/tracks", params={"ids": track_id}
        )

    async def get_recently_played(self, limit: int = 20) -> list[dict]:
        data = await self._request(
            "GET", "/me/player/recently-played",
            params={"limit": max(1, min(limit, 50))},
        )
        return [
            {"played_at": item.get("played_at"), "track": _normalize_track(item["track"])}
            for item in data.get("items") or [] if item.get("track")
        ]

    async def get_playlist_metadata(self, playlist_id: str) -> dict:
        """Return header-only metadata for a playlist (no track listing).

        Cheap call used by the sync engine to compare snapshot_id and skip
        unchanged playlists before pulling track contents.

        Requires `playlist-read-private` (and `playlist-read-collaborative`
        for collaborative playlists).
        """
        data = await self._request(
            "GET",
            f"/playlists/{playlist_id}",
            params={"fields": "id,name,description,public,collaborative,snapshot_id,owner(id,display_name),tracks(total),external_urls"},
        )
        owner = data.get("owner", {}) or {}
        return {
            "id": data["id"],
            "name": data["name"],
            "description": data.get("description"),
            "url": data.get("external_urls", {}).get("spotify"),
            "public": data.get("public"),
            "collaborative": data.get("collaborative"),
            "snapshot_id": data.get("snapshot_id"),
            "owner_id": owner.get("id"),
            "owner_name": owner.get("display_name"),
            "track_count": data.get("tracks", {}).get("total"),
        }

    async def get_playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Return every track on a playlist as a normalized list.

        Paginates at 100 items per page (Spotify's max). Each track dict
        includes the ISRC (from `external_ids.isrc`) which is the
        cross-service matching key for Spotify <-> Tidal sync.

        Local (non-Spotify) tracks added from a user's machine are skipped —
        they have no usable id/uri/isrc and can't be synced.

        Requires `playlist-read-private` (and `playlist-read-collaborative`
        for collaborative playlists).
        """
        # Limit the response payload with `fields` — playlist track responses
        # are large and most fields are unused by the sync engine.
        fields = (
            "next,items("
            "added_at,is_local,"
            "track(id,uri,name,duration_ms,external_ids(isrc),"
            "artists(id,name),album(id,name))"
            ")"
        )
        results: list[dict] = []
        offset = 0
        while True:
            data = await self._request(
                "GET",
                f"/playlists/{playlist_id}/tracks",
                params={"limit": 100, "offset": offset, "fields": fields},
            )
            items = data.get("items", []) or []
            if not items:
                break
            for item in items:
                track = item.get("track") or {}
                if item.get("is_local") or not track.get("id"):
                    # Local file or removed/unplayable track — skip; can't sync.
                    continue
                normalized = _normalize_track(track)
                normalized["added_at"] = item.get("added_at")
                results.append(normalized)
            if not data.get("next"):
                break
            offset += len(items)
        return results

    # ---- parsing helpers ----

    @staticmethod
    def parse_track_ref(ref: str) -> str:
        """Accept a Spotify track URI, open.spotify.com URL, or bare 22-char
        track ID; return a normalized `spotify:track:<id>` URI."""
        s = ref.strip()
        if s.startswith("spotify:track:"):
            return s
        m = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", s)
        if m:
            return f"spotify:track:{m.group(1)}"
        if re.fullmatch(r"[A-Za-z0-9]{22}", s):
            return f"spotify:track:{s}"
        raise ValueError(f"Not a recognizable Spotify track reference: {ref!r}")

    @staticmethod
    def parse_playlist_id(ref: str) -> str | None:
        """Accept a Spotify playlist URI, URL, or 22-char ID; return the bare
        ID. Returns None if the input doesn't look like any of those (caller
        should then try resolving by name)."""
        s = ref.strip()
        if s.startswith("spotify:playlist:"):
            return s.split(":")[-1]
        m = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", s)
        if m:
            return m.group(1)
        if re.fullmatch(r"[A-Za-z0-9]{22}", s):
            return s
        return None

    async def resolve_playlist(self, ref: str) -> dict | None:
        """Resolve a user-supplied playlist reference (URL, URI, ID, or name)
        to a `{id, name, url}` dict. Name resolution walks the user's
        playlists page by page and returns on first case-insensitive match,
        so users with more than 50 playlists still resolve correctly."""
        pid = self.parse_playlist_id(ref)
        if pid:
            data = await self._request("GET", f"/playlists/{pid}", params={"fields": "id,name,external_urls"})
            return {
                "id": data["id"],
                "name": data["name"],
                "url": data.get("external_urls", {}).get("spotify"),
            }
        lowered = ref.strip().lower()
        offset = 0
        while True:
            data = await self._request(
                "GET", "/me/playlists", params={"limit": 50, "offset": offset}
            )
            items = data.get("items", [])
            if not items:
                return None
            for p in items:
                if (p.get("name") or "").lower() == lowered:
                    return {
                        "id": p["id"],
                        "name": p["name"],
                        "url": p.get("external_urls", {}).get("spotify"),
                    }
            if not data.get("next"):
                return None
            offset += len(items)
