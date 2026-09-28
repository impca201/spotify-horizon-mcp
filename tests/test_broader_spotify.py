"""Contract checks for the Horizon fork's broader Spotify tools."""

from unittest.mock import AsyncMock

import pytest

from clients.spotify import SpotifyClient


@pytest.fixture
async def client():
    c = SpotifyClient("id", "secret", "refresh")
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_search_tracks_clamps_dev_mode_limit_and_returns_uris(client):
    client._request = AsyncMock(return_value={
        "tracks": {"items": [{
            "id": "abc", "name": "Song", "uri": "spotify:track:abc",
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album"},
            "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
        }]}
    })
    result = await client.search_catalog("Song", "track", 50)
    assert result[0]["uri"] == "spotify:track:abc"
    assert result[0]["artists"] == ["Artist"]
    client._request.assert_awaited_once_with(
        "GET", "/search", params={"q": "Song", "type": "track", "limit": 10}
    )


@pytest.mark.asyncio
async def test_play_track_normalizes_url_and_device(client):
    client._request = AsyncMock(return_value={})
    await client.start_playback(
        "https://open.spotify.com/track/1234567890123456789012",
        "device-1",
    )
    client._request.assert_awaited_once_with(
        "PUT", "/me/player/play",
        params={"device_id": "device-1"},
        json_body={"uris": ["spotify:track:1234567890123456789012"]},
    )


@pytest.mark.asyncio
async def test_queue_and_library_use_track_reference(client):
    client._request = AsyncMock(return_value={})
    track = "spotify:track:1234567890123456789012"
    await client.add_to_queue(track)
    await client.save_track(track, save=False)
    assert client._request.await_args_list[0].args == ("POST", "/me/player/queue")
    assert client._request.await_args_list[0].kwargs == {"params": {"uri": track}}
    assert client._request.await_args_list[1].args == ("DELETE", "/me/library")
    assert client._request.await_args_list[1].kwargs == {
        "params": {"uris": track}
    }


@pytest.mark.asyncio
async def test_invalid_controls_never_call_spotify(client):
    client._request = AsyncMock()
    with pytest.raises(ValueError):
        await client.set_volume(120)
    with pytest.raises(ValueError):
        await client.playback_action("delete")
    with pytest.raises(ValueError):
        await client.search_catalog("x", "user")
    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_playback_handles_no_active_device(client):
    client._request = AsyncMock(return_value={})
    assert await client.get_playback_state() == {
        "is_playing": False, "progress_ms": None,
        "device": None, "track": None, "context": None,
    }
