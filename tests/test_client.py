"""HTTP-mocked tests for the Spotify OAuth client.

The OAuth refresh-token flow in clients/spotify.py is the highest-risk code in
this server: a double-checked lock around token refresh, refresh-token rotation
that must persist, a 401 force-refresh-and-retry path, and 429 Retry-After
honoring with a cap. These tests pin that behavior with respx so no live
Spotify call is ever made.
"""

import asyncio
import os

os.environ.setdefault("SPOTIFY_CLIENT_ID", "test-id")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test-secret")
os.environ.setdefault("SPOTIFY_REFRESH_TOKEN", "test-refresh")

import httpx
import pytest
import respx

from clients.spotify import (
    SPOTIFY_API_BASE,
    SPOTIFY_TOKEN_URL,
    SpotifyClient,
    SpotifyError,
)


def _token_payload(access="access-1", expires_in=3600, refresh=None):
    body = {"access_token": access, "token_type": "Bearer", "expires_in": expires_in}
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


@pytest.fixture
async def client():
    c = SpotifyClient("cid", "csecret", "refresh-0")
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# token refresh on expiry
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_ensure_token_refreshes_when_no_token(client):
    token_route = respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload(access="fresh"))
    )
    await client._ensure_token()
    assert token_route.called
    assert client._access_token == "fresh"
    # expiry is roughly now + expires_in
    assert client._expires_at > 0


@respx.mock
@pytest.mark.asyncio
async def test_ensure_token_refreshes_when_expired(client):
    token_route = respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload(access="new"))
    )
    # Pretend we already hold a token that is already inside the 60s skew window.
    client._access_token = "stale"
    client._expires_at = 0.0  # long expired
    await client._ensure_token()
    assert token_route.called
    assert client._access_token == "new"


@respx.mock
@pytest.mark.asyncio
async def test_ensure_token_noop_when_valid(client):
    token_route = respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload())
    )
    import time

    client._access_token = "still-good"
    client._expires_at = time.time() + 3600
    await client._ensure_token()
    assert not token_route.called
    assert client._access_token == "still-good"


@respx.mock
@pytest.mark.asyncio
async def test_refresh_failure_raises(client):
    respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(400, text="invalid_grant")
    )
    with pytest.raises(SpotifyError) as exc:
        await client._ensure_token()
    assert "400" in str(exc.value)


# ---------------------------------------------------------------------------
# double-checked locking: concurrent callers refresh exactly once
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_double_checked_lock_refreshes_once(client):
    """N concurrent _ensure_token callers must hit the token endpoint once.

    Spotify rotates refresh tokens on use, so two concurrent refreshes would
    invalidate each other. The double-checked lock must collapse them to one.
    """
    call_count = 0

    async def _token_handler(request):
        nonlocal call_count
        call_count += 1
        # Yield control so other waiters pile up on the lock before we answer.
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=_token_payload(access="once"))

    respx.post(SPOTIFY_TOKEN_URL).mock(side_effect=_token_handler)

    await asyncio.gather(*[client._ensure_token() for _ in range(8)])

    assert call_count == 1
    assert client._access_token == "once"


# ---------------------------------------------------------------------------
# refresh-token rotation persisted
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_refresh_token_rotation_persisted(client):
    """When Spotify returns a new refresh_token, the client must store it and
    use it on the next refresh."""
    seen_refresh_tokens = []

    async def _token_handler(request):
        # respx gives us the raw content; decode the form body.
        body = request.content.decode()
        for part in body.split("&"):
            if part.startswith("refresh_token="):
                seen_refresh_tokens.append(part.split("=", 1)[1])
        # First call rotates to refresh-1, second to refresh-2.
        idx = len(seen_refresh_tokens)
        return httpx.Response(
            200, json=_token_payload(access=f"a{idx}", refresh=f"refresh-{idx}")
        )

    respx.post(SPOTIFY_TOKEN_URL).mock(side_effect=_token_handler)

    assert client._refresh_token == "refresh-0"
    await client._refresh_access_token()
    assert client._refresh_token == "refresh-1"
    await client._refresh_access_token()
    assert client._refresh_token == "refresh-2"
    # The rotated token from round 1 was actually sent on round 2.
    assert seen_refresh_tokens == ["refresh-0", "refresh-1"]


@respx.mock
@pytest.mark.asyncio
async def test_refresh_token_unchanged_when_not_returned(client):
    respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload(access="x"))  # no refresh
    )
    await client._refresh_access_token()
    assert client._refresh_token == "refresh-0"


# ---------------------------------------------------------------------------
# 401 -> force refresh -> retry succeeds
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_401_forces_refresh_and_retries(client):
    """A 401 on the first attempt clears the token, re-refreshes, and retries
    the same request, which then succeeds."""
    # Seed a valid-looking token so the first _request doesn't refresh up front.
    import time

    client._access_token = "expired-token"
    client._expires_at = time.time() + 3600

    refresh_route = respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload(access="rescued"))
    )

    attempts = {"n": 0}

    async def _api_handler(request):
        attempts["n"] += 1
        auth = request.headers.get("Authorization")
        if attempts["n"] == 1:
            assert auth == "Bearer expired-token"
            return httpx.Response(401, json={"error": {"message": "expired"}})
        # second attempt uses the freshly refreshed token
        assert auth == "Bearer rescued"
        return httpx.Response(200, json={"id": "user-42"})

    respx.get(f"{SPOTIFY_API_BASE}/me").mock(side_effect=_api_handler)

    data = await client._request("GET", "/me")
    assert data == {"id": "user-42"}
    assert attempts["n"] == 2
    assert refresh_route.called


@respx.mock
@pytest.mark.asyncio
async def test_401_twice_raises(client):
    """If the retried request still 401s, the error surfaces (no infinite loop)."""
    import time

    client._access_token = "tok"
    client._expires_at = time.time() + 3600
    respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload(access="tok2"))
    )
    respx.get(f"{SPOTIFY_API_BASE}/me").mock(
        return_value=httpx.Response(401, text="still unauthorized")
    )
    with pytest.raises(SpotifyError) as exc:
        await client._request("GET", "/me")
    assert "401" in str(exc.value)


# ---------------------------------------------------------------------------
# 429 Retry-After honored and capped
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_429_respects_retry_after(client, monkeypatch):
    import time

    client._access_token = "tok"
    client._expires_at = time.time() + 3600
    respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload())
    )

    sleeps = []

    async def _fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr("clients.spotify.asyncio.sleep", _fake_sleep)

    calls = {"n": 0}

    async def _handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")
        return httpx.Response(200, json={"id": "ok"})

    respx.get(f"{SPOTIFY_API_BASE}/me").mock(side_effect=_handler)

    data = await client._request("GET", "/me")
    assert data == {"id": "ok"}
    assert sleeps == [5.0]


@respx.mock
@pytest.mark.asyncio
async def test_429_retry_after_is_capped(client, monkeypatch):
    """A huge Retry-After must be capped to 30s so a tool call can't hang."""
    import time

    client._access_token = "tok"
    client._expires_at = time.time() + 3600
    respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload())
    )

    sleeps = []

    async def _fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr("clients.spotify.asyncio.sleep", _fake_sleep)

    calls = {"n": 0}

    async def _handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "9999"}, text="rate")
        return httpx.Response(200, json={"ok": True})

    respx.get(f"{SPOTIFY_API_BASE}/me").mock(side_effect=_handler)

    await client._request("GET", "/me")
    assert sleeps == [30.0]


@respx.mock
@pytest.mark.asyncio
async def test_429_bad_retry_after_defaults(client, monkeypatch):
    """A non-numeric Retry-After header falls back to a 1s delay."""
    import time

    client._access_token = "tok"
    client._expires_at = time.time() + 3600
    respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload())
    )
    sleeps = []

    async def _fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr("clients.spotify.asyncio.sleep", _fake_sleep)
    calls = {"n": 0}

    async def _handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "soon"}, text="rate")
        return httpx.Response(200, json={"ok": True})

    respx.get(f"{SPOTIFY_API_BASE}/me").mock(side_effect=_handler)
    await client._request("GET", "/me")
    assert sleeps == [1.0]


# ---------------------------------------------------------------------------
# tool-level behavior with a mocked transport (search_artists shaping)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_search_artists_shapes_and_prefers_exact(client):
    respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload())
    )
    respx.get(f"{SPOTIFY_API_BASE}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "artists": {
                    "items": [
                        {
                            "id": "near",
                            "name": "Radiohead Tribute",
                            "popularity": 40,
                            "genres": ["tribute"],
                            "followers": {"total": 10},
                            "external_urls": {"spotify": "https://x/near"},
                        },
                        {
                            "id": "exact",
                            "name": "Radiohead",
                            "popularity": 90,
                            "genres": ["rock"],
                            "followers": {"total": 999},
                            "external_urls": {"spotify": "https://x/exact"},
                        },
                    ]
                }
            },
        )
    )
    results = await client.search_artists("radiohead", limit=5)
    assert results[0]["id"] == "exact"  # exact case-insensitive match floated up
    assert results[0]["name"] == "Radiohead"
    assert results[0]["followers"] == 999
    assert results[0]["url"] == "https://x/exact"
    assert set(results[0].keys()) == {
        "id",
        "name",
        "popularity",
        "genres",
        "followers",
        "url",
    }


@respx.mock
@pytest.mark.asyncio
async def test_search_track_by_isrc_returns_normalized(client):
    respx.post(SPOTIFY_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_payload())
    )
    respx.get(f"{SPOTIFY_API_BASE}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "t1",
                            "uri": "spotify:track:t1",
                            "name": "Song",
                            "artists": [{"id": "a1", "name": "Artist"}],
                            "album": {"id": "al1", "name": "Album"},
                            "external_ids": {"isrc": "USABC1234567"},
                            "duration_ms": 200000,
                        }
                    ]
                }
            },
        )
    )
    tracks = await client.search_track_by_isrc("USABC1234567")
    assert len(tracks) == 1
    t = tracks[0]
    assert t["isrc"] == "USABC1234567"
    assert t["uri"] == "spotify:track:t1"
    assert t["artists"] == [{"id": "a1", "name": "Artist"}]
    assert t["album"] == {"id": "al1", "name": "Album"}


# ---------------------------------------------------------------------------
# successful responses whose body is not JSON
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"\n", b"OK"])
async def test_successful_player_command_with_non_json_body_is_ok(client, body):
    client._access_token, client._expires_at = "tok", 9e12
    respx.put(f"{SPOTIFY_API_BASE}/me/player/seek").mock(
        return_value=httpx.Response(200, content=body)
    )
    assert await client._request("PUT", "/me/player/seek", params={"position_ms": 1}) == {}


@respx.mock
@pytest.mark.asyncio
async def test_non_json_body_on_a_read_is_an_error(client):
    client._access_token, client._expires_at = "tok", 9e12
    respx.get(f"{SPOTIFY_API_BASE}/me/player").mock(
        return_value=httpx.Response(200, content=b"<html>oops</html>")
    )
    with pytest.raises(SpotifyError, match="invalid JSON"):
        await client._request("GET", "/me/player")


@pytest.mark.asyncio
async def test_playback_state_reports_shuffle_and_repeat(client):
    from unittest.mock import AsyncMock
    client._request = AsyncMock(return_value={
        "is_playing": True, "shuffle_state": True, "repeat_state": "track",
    })
    state = await client.get_playback_state()
    assert state["shuffle"] is True and state["repeat"] == "track"
