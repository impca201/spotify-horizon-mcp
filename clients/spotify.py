"""Spotify Web API client with OAuth 2.0 refresh-token flow."""

import asyncio
import base64
import re
import time

import httpx

SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

#: Item types /search accepts that this client supports.
SEARCH_TYPES = frozenset(
    {"track", "album", "artist", "playlist", "show", "episode", "audiobook"}
)
#: Item types that can be saved to the library by URI (/me/library).
LIBRARY_KINDS = ("track", "album", "artist", "show", "episode", "audiobook", "playlist")
#: Item types playback can start from as a context.
CONTEXT_KINDS = ("album", "artist", "playlist", "show")
REPEAT_STATES = ("track", "context", "off")
TIME_RANGES = ("short_term", "medium_term", "long_term")


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


#: Fields copied through as-is when Spotify returns them. Development Mode strips
#: several (popularity, followers, genres), so every one is optional.
_SUMMARY_FIELDS = (
    "album_type", "release_date", "total_tracks", "total_episodes", "total_chapters",
    "duration_ms", "explicit", "popularity", "genres", "publisher", "label",
    "languages", "media_type", "episode_number", "chapter_number", "track_number",
    "disc_number", "is_playable",
)


def _summarize(item: dict) -> dict:
    """Compact, uniform shape for any catalog object (track, album, artist, show,
    episode, audiobook, chapter). Absent fields are left out rather than null."""
    out: dict = {
        "id": item.get("id"),
        "uri": item.get("uri"),
        "name": item.get("name"),
        "type": item.get("type"),
        "url": (item.get("external_urls") or {}).get("spotify"),
    }
    for key in ("artists", "authors", "narrators"):
        names = [p.get("name") for p in item.get(key) or [] if p]
        if names:
            out[key] = names
    album = item.get("album")
    if isinstance(album, dict) and album.get("name"):
        out["album"] = album["name"]
    followers = item.get("followers")
    if isinstance(followers, dict) and followers.get("total") is not None:
        out["followers"] = followers["total"]
    isrc = (item.get("external_ids") or {}).get("isrc")
    if isrc:
        out["isrc"] = isrc
    description = item.get("description")
    if description:
        out["description"] = description[:500]
    images = item.get("images") or []
    if images and images[0] and images[0].get("url"):
        out["image"] = images[0]["url"]
    for key in _SUMMARY_FIELDS:
        if item.get(key) is not None:
            out[key] = item[key]
    return out


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
            if resp.status_code == 204 or not resp.content.strip():
                return {}
            try:
                return resp.json()
            except ValueError:
                # Player commands (seek, shuffle, repeat) can succeed with a
                # body that is not JSON. The command already worked, so only a
                # read, where the body is the point, treats this as a failure.
                if method != "GET":
                    return {}
                raise SpotifyError(f"Spotify API {method} {path} returned invalid JSON") from None
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
        collaborative: bool | None = None,
    ) -> None:
        """Rename a playlist, change its description, or toggle visibility."""
        body: dict = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if public is not None:
            body["public"] = public
        if collaborative is not None:
            body["collaborative"] = collaborative
        if not body:
            return
        await self._request("PUT", f"/playlists/{playlist_id}", json_body=body)

    async def unfollow_playlist(self, playlist_id: str) -> None:
        """Spotify's equivalent of "delete a playlist" — removes it from the
        user's library by unfollowing. The playlist object itself persists on
        Spotify's side but drops out of the user's view."""
        await self._request(
            "DELETE", "/me/library", params={"uris": f"spotify:playlist:{playlist_id}"}
        )

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
                        "track_count": (p.get("items") or p.get("tracks") or {}).get("total"),
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
        if item_type not in SEARCH_TYPES:
            raise ValueError(f"item_type must be one of: {', '.join(sorted(SEARCH_TYPES))}")
        if not query.strip():
            raise ValueError("query cannot be empty")
        data = await self._request(
            "GET", "/search",
            params={"q": query.strip(), "type": item_type, "limit": max(1, min(limit, 10))},
        )
        collection = (data.get(item_type + "s") or {}).get("items") or []
        results = [
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
        if item_type == "audiobook":
            # Spotify lists audiobooks under show URIs and URLs here, but the
            # audiobook endpoints and library want the audiobook form.
            for r in results:
                if r["id"]:
                    r["uri"] = f"spotify:audiobook:{r['id']}"
                if r["url"]:
                    r["url"] = r["url"].replace("/show/", "/audiobook/")
        return results

    async def get_playback_state(self) -> dict:
        data = await self._request("GET", "/me/player")
        item = data.get("item") or {}
        return {
            "is_playing": data.get("is_playing", False),
            "progress_ms": data.get("progress_ms"),
            "device": data.get("device"),
            "track": _normalize_track(item) if item.get("type") == "track" else None,
            "context": data.get("context"),
            "shuffle": data.get("shuffle_state"),
            "repeat": data.get("repeat_state"),
        }

    async def get_devices(self) -> list[dict]:
        data = await self._request("GET", "/me/player/devices")
        return data.get("devices") or []

    async def start_playback(
        self,
        track: str | None = None,
        device_id: str | None = None,
        context: str | None = None,
        tracks: list[str] | None = None,
    ) -> None:
        """Resume playback, or start a track, a list of tracks, or a context
        (album, artist, playlist or show). Only one of the three may be given."""
        if sum(bool(x) for x in (track, tracks, context)) > 1:
            raise ValueError("Give only one of track, tracks or context")
        body: dict | None = None
        if track:
            body = {"uris": [self.parse_track_ref(track)]}
        elif tracks:
            body = {"uris": [self.parse_track_ref(t) for t in tracks]}
        elif context:
            body = {"context_uri": self.parse_uri(context, CONTEXT_KINDS)}
        params = {"device_id": device_id} if device_id else None
        await self._request("PUT", "/me/player/play", params=params, json_body=body)

    async def seek(self, position_ms: int) -> None:
        if position_ms < 0:
            raise ValueError("position_ms must be 0 or greater")
        await self._request(
            "PUT", "/me/player/seek", params={"position_ms": position_ms}
        )

    async def set_shuffle(self, state: bool) -> None:
        await self._request(
            "PUT", "/me/player/shuffle", params={"state": "true" if state else "false"}
        )

    async def set_repeat(self, state: str) -> None:
        if state not in REPEAT_STATES:
            raise ValueError("state must be track, context or off")
        await self._request("PUT", "/me/player/repeat", params={"state": state})

    async def transfer_playback(self, device_id: str, play: bool = False) -> None:
        if not device_id.strip():
            raise ValueError("device_id cannot be empty")
        await self._request(
            "PUT", "/me/player", json_body={"device_ids": [device_id], "play": play}
        )

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
        uri = self.parse_track_ref(track)
        await self._request(
            "PUT" if save else "DELETE", "/me/library", params={"uris": uri}
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
            params={"fields": "id,name,description,public,collaborative,snapshot_id,owner(id,display_name),items(total),external_urls"},
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
            "track_count": (data.get("items") or data.get("tracks") or {}).get("total"),
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
        results: list[dict] = []
        offset = 0
        while True:
            data = await self._request(
                "GET",
                f"/playlists/{playlist_id}/items",
                params={"limit": 50, "offset": offset},
            )
            items = data.get("items", []) or []
            if not items:
                break
            for item in items:
                track = item.get("item") or item.get("track") or {}
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

    # ---- catalog lookups ----

    async def _paged(
        self, path: str, limit: int, params: dict | None = None, key: str | None = None
    ) -> list[dict]:
        """Collect up to `limit` items from an offset-paginated endpoint.

        `key` names the wrapper object when the page sits under one (e.g.
        /me/following puts it under "artists"); otherwise items are top level.
        """
        limit = max(1, limit)
        results: list[dict] = []
        offset = 0
        while len(results) < limit:
            page_params = dict(params or {})
            page_params.update({"limit": min(50, limit - len(results)), "offset": offset})
            data = await self._request("GET", path, params=page_params)
            page = (data.get(key) or {}) if key else data
            items = page.get("items") or []
            if not items:
                break
            results.extend(i for i in items if i)
            if not page.get("next"):
                break
            offset += len(items)
        return results[:limit]

    async def get_profile(self) -> dict:
        """The authenticated user's profile. Email needs user-read-email and is
        deliberately not returned."""
        data = await self._request("GET", "/me")
        return {
            "id": data.get("id"),
            "display_name": data.get("display_name"),
            "country": data.get("country"),
            "product": data.get("product"),
            "followers": (data.get("followers") or {}).get("total"),
            "url": (data.get("external_urls") or {}).get("spotify"),
        }

    async def get_track(self, ref: str, market: str | None = None) -> dict:
        data = await self._request(
            "GET", f"/tracks/{self.parse_id(ref, 'track')}",
            params={"market": market} if market else None,
        )
        return _summarize(data)

    async def get_album(self, ref: str, market: str | None = None) -> dict:
        """Album metadata with its full track listing."""
        album_id = self.parse_id(ref, "album")
        data = await self._request(
            "GET", f"/albums/{album_id}", params={"market": market} if market else None
        )
        page = data.get("tracks") or {}
        tracks = [t for t in page.get("items") or [] if t]
        if page.get("next"):
            tracks = await self._paged(
                f"/albums/{album_id}/tracks", 500,
                params={"market": market} if market else None,
            )
        out = _summarize(data)
        out["tracks"] = [_summarize(t) for t in tracks]
        return out

    async def get_artist(self, ref: str) -> dict:
        return _summarize(
            await self._request("GET", f"/artists/{self.parse_id(ref, 'artist')}")
        )

    async def get_artist_albums(
        self,
        ref: str,
        include_groups: str = "album,single",
        limit: int = 20,
        market: str | None = None,
    ) -> list[dict]:
        """Albums by an artist. include_groups is a comma-separated subset of
        album, single, appears_on, compilation."""
        groups = [g.strip() for g in include_groups.split(",") if g.strip()]
        bad = set(groups) - {"album", "single", "appears_on", "compilation"}
        if bad or not groups:
            raise ValueError(
                "include_groups must be a comma-separated list of "
                "album, single, appears_on, compilation"
            )
        params: dict = {"include_groups": ",".join(groups)}
        if market:
            params["market"] = market
        items = await self._paged(
            f"/artists/{self.parse_id(ref, 'artist')}/albums", min(limit, 200), params
        )
        return [_summarize(a) for a in items]

    async def get_show(self, ref: str, market: str | None = None) -> dict:
        data = await self._request(
            "GET", f"/shows/{self.parse_id(ref, 'show')}",
            params={"market": market} if market else None,
        )
        return _summarize(data)

    async def get_show_episodes(
        self, ref: str, limit: int = 20, market: str | None = None
    ) -> list[dict]:
        items = await self._paged(
            f"/shows/{self.parse_id(ref, 'show')}/episodes", min(limit, 200),
            {"market": market} if market else None,
        )
        return [_summarize(e) for e in items]

    async def get_episode(self, ref: str, market: str | None = None) -> dict:
        data = await self._request(
            "GET", f"/episodes/{self.parse_id(ref, 'episode')}",
            params={"market": market} if market else None,
        )
        return _summarize(data)

    async def get_audiobook(self, ref: str, market: str = "US") -> dict:
        """Audiobook metadata. Spotify serves audiobooks in only a few markets
        (US, UK, CA, IE, NZ, AU), so market defaults to US."""
        data = await self._request(
            "GET", f"/audiobooks/{self.parse_id(ref, 'audiobook')}",
            params={"market": market},
        )
        out = _summarize(data)
        out["chapters"] = [
            _summarize(c) for c in (data.get("chapters") or {}).get("items") or [] if c
        ]
        return out

    async def get_playlist_cover(self, playlist_id: str) -> list[dict]:
        data = await self._request("GET", f"/playlists/{playlist_id}/images")
        images = data if isinstance(data, list) else data.get("images") or []
        return [
            {"url": i.get("url"), "width": i.get("width"), "height": i.get("height")}
            for i in images if i
        ]

    # ---- user data ----

    async def get_saved(self, kind: str, limit: int = 20) -> list[dict]:
        """Saved albums, shows, episodes or audiobooks, newest first."""
        paths = {
            "album": "/me/albums", "show": "/me/shows",
            "episode": "/me/episodes", "audiobook": "/me/audiobooks",
        }
        if kind not in paths:
            raise ValueError("kind must be album, show, episode or audiobook")
        items = await self._paged(paths[kind], min(limit, 200))
        # Each entry wraps the item under its own key, next to added_at.
        return [
            {"added_at": i.get("added_at"), **_summarize(i.get(kind) or i.get("item") or i)}
            for i in items
        ]

    async def check_saved(self, refs: list[str]) -> list[dict]:
        """Whether each item is in the library. Refs must be URIs or URLs, since
        a bare ID does not say what kind of item it is."""
        uris = [self.parse_uri(r, LIBRARY_KINDS) for r in refs]
        flags: list[bool] = []
        for i in range(0, len(uris), 40):
            data = await self._request(
                "GET", "/me/library/contains",
                params={"uris": ",".join(uris[i : i + 40])},
            )
            flags.extend(bool(f) for f in (data if isinstance(data, list) else []))
        return [{"uri": u, "saved": f} for u, f in zip(uris, flags)]

    async def set_saved(self, refs: list[str], save: bool = True) -> int:
        """Save or remove items (tracks, albums, artists, shows, episodes,
        audiobooks, playlists) in one go. Chunked at 40 URIs per request."""
        uris = [self.parse_uri(r, LIBRARY_KINDS) for r in refs]
        for i in range(0, len(uris), 40):
            await self._request(
                "PUT" if save else "DELETE", "/me/library",
                params={"uris": ",".join(uris[i : i + 40])},
            )
        return len(uris)

    async def get_top_items(
        self, kind: str = "tracks", time_range: str = "medium_term", limit: int = 20
    ) -> list[dict]:
        """Your most listened-to artists or tracks. Needs user-top-read."""
        if kind not in ("artists", "tracks"):
            raise ValueError("kind must be artists or tracks")
        if time_range not in TIME_RANGES:
            raise ValueError("time_range must be short_term, medium_term or long_term")
        data = await self._request(
            "GET", f"/me/top/{kind}",
            params={"time_range": time_range, "limit": max(1, min(limit, 50))},
        )
        return [_summarize(i) for i in data.get("items") or [] if i]

    async def get_followed_artists(self, limit: int = 20) -> list[dict]:
        """Artists you follow. Needs user-follow-read."""
        data = await self._request(
            "GET", "/me/following",
            params={"type": "artist", "limit": max(1, min(limit, 50))},
        )
        return [_summarize(a) for a in (data.get("artists") or {}).get("items") or [] if a]

    # ---- playlist contents ----

    async def reorder_playlist_items(
        self, playlist_id: str, range_start: int, insert_before: int,
        range_length: int = 1,
    ) -> str | None:
        """Move a block of items inside a playlist. Returns the new snapshot_id."""
        if min(range_start, insert_before) < 0 or range_length < 1:
            raise ValueError(
                "range_start and insert_before must be 0 or greater, range_length 1 or more"
            )
        data = await self._request(
            "PUT", f"/playlists/{playlist_id}/items",
            json_body={
                "range_start": range_start,
                "insert_before": insert_before,
                "range_length": range_length,
            },
        )
        return data.get("snapshot_id")

    async def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> int:
        """Replace everything on a playlist. The first 100 URIs replace the
        contents, any further ones are appended (Spotify caps a request at 100)."""
        first, rest = uris[:100], uris[100:]
        await self._request(
            "PUT", f"/playlists/{playlist_id}/items", json_body={"uris": first}
        )
        if rest:
            await self.add_tracks(playlist_id, rest)
        return len(uris)

    async def add_tracks_at(
        self, playlist_id: str, uris: list[str], position: int | None = None
    ) -> int:
        """Add tracks, optionally inserting at a zero-based position. Without a
        position this is add_tracks."""
        if position is None:
            return await self.add_tracks(playlist_id, uris)
        if position < 0:
            raise ValueError("position must be 0 or greater")
        # Each chunk lands right after the previous one, so order is preserved.
        for i in range(0, len(uris), 100):
            await self._request(
                "POST", f"/playlists/{playlist_id}/items",
                json_body={"uris": uris[i : i + 100], "position": position + i},
            )
        return len(uris)

    # ---- parsing helpers ----

    _REF_RE = re.compile(
        r"(?:spotify:|open\.spotify\.com/(?:intl-[A-Za-z-]+/)?)"
        r"(track|album|artist|show|episode|audiobook|playlist)[:/]([A-Za-z0-9]{22})"
    )

    @classmethod
    def parse_uri(cls, ref: str, kinds: tuple[str, ...] = LIBRARY_KINDS) -> str:
        """Normalize a Spotify URI or open.spotify.com URL to `spotify:<kind>:<id>`.

        Bare IDs are rejected: they do not say what kind of item they are.
        """
        m = cls._REF_RE.search(ref.strip())
        if not m or m.group(1) not in kinds:
            raise ValueError(
                f"Not a recognizable Spotify {'/'.join(kinds)} URI or URL: {ref!r}"
            )
        return f"spotify:{m.group(1)}:{m.group(2)}"

    @classmethod
    def parse_id(cls, ref: str, kind: str) -> str:
        """Accept a URI, URL or bare 22-char ID of the given kind; return the ID."""
        s = ref.strip()
        if re.fullmatch(r"[A-Za-z0-9]{22}", s):
            return s
        # Audiobooks also circulate as show URIs and URLs (older search results).
        kinds = (kind, "show") if kind == "audiobook" else (kind,)
        return cls.parse_uri(s, kinds).rsplit(":", 1)[1]

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
