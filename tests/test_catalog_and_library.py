"""Contract checks for the catalog lookup, library, playback and playlist-content tools."""

import json
import os
from unittest.mock import AsyncMock

os.environ.setdefault("SPOTIFY_CLIENT_ID", "test-id")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test-secret")
os.environ.setdefault("SPOTIFY_REFRESH_TOKEN", "test-refresh")

import pytest

import server
from clients.spotify import SpotifyClient, SpotifyError, _summarize

ID = "1234567890123456789012"


@pytest.fixture
async def client():
    c = SpotifyClient("id", "secret", "refresh")
    yield c
    await c.close()


# ---- reference parsing ----


class TestParseUri:
    def test_uri_and_url_forms(self):
        assert SpotifyClient.parse_uri(f"spotify:album:{ID}") == f"spotify:album:{ID}"
        assert SpotifyClient.parse_uri(f"https://open.spotify.com/artist/{ID}?si=x") == f"spotify:artist:{ID}"
        assert SpotifyClient.parse_uri(f"https://open.spotify.com/intl-nl/show/{ID}") == f"spotify:show:{ID}"

    def test_bare_id_is_rejected(self):
        with pytest.raises(ValueError):
            SpotifyClient.parse_uri(ID)

    def test_kind_filter(self):
        with pytest.raises(ValueError):
            SpotifyClient.parse_uri(f"spotify:user:{ID}")
        with pytest.raises(ValueError):
            SpotifyClient.parse_uri(f"spotify:track:{ID}", ("album",))

    def test_parse_id_accepts_bare_id_but_checks_kind(self):
        assert SpotifyClient.parse_id(ID, "album") == ID
        assert SpotifyClient.parse_id(f"spotify:album:{ID}", "album") == ID
        with pytest.raises(ValueError):
            SpotifyClient.parse_id(f"spotify:track:{ID}", "album")


def test_summarize_omits_missing_fields_and_keeps_present_ones():
    out = _summarize({
        "id": "a", "uri": "spotify:artist:a", "name": "A", "type": "artist",
        "followers": {"total": 5}, "images": [{"url": "http://img"}],
        "description": "x" * 900, "popularity": None,
    })
    assert out["followers"] == 5 and out["image"] == "http://img"
    assert len(out["description"]) == 500
    assert "popularity" not in out and "genres" not in out


# ---- lookups ----


@pytest.mark.asyncio
async def test_get_album_returns_tracks_and_fetches_remaining_pages(client):
    client._request = AsyncMock(side_effect=[
        {"id": "al", "name": "Album", "type": "album",
         "tracks": {"items": [{"id": "t1", "name": "One"}], "next": "more"}},
        {"items": [{"id": "t1", "name": "One"}, {"id": "t2", "name": "Two"}]},
    ])
    out = await client.get_album(ID)
    assert [t["name"] for t in out["tracks"]] == ["One", "Two"]
    assert client._request.await_args_list[0].args == ("GET", f"/albums/{ID}")


@pytest.mark.asyncio
async def test_artist_albums_validates_groups_and_paginates(client):
    client._request = AsyncMock()
    with pytest.raises(ValueError):
        await client.get_artist_albums(ID, include_groups="nonsense")
    client._request.assert_not_awaited()

    client._request = AsyncMock(side_effect=[
        {"items": [{"id": str(i), "name": f"A{i}"} for i in range(50)], "next": "n"},
        {"items": [{"id": "x", "name": "last"}], "next": None},
    ])
    out = await client.get_artist_albums(f"spotify:artist:{ID}", limit=60)
    assert len(out) == 51
    first, second = client._request.await_args_list
    assert first.kwargs["params"] == {"include_groups": "album,single", "limit": 50, "offset": 0}
    assert second.kwargs["params"]["offset"] == 50


@pytest.mark.asyncio
async def test_audiobook_defaults_to_us_market(client):
    client._request = AsyncMock(return_value={
        "id": "b", "name": "Book", "type": "audiobook",
        "authors": [{"name": "Au"}], "chapters": {"items": [{"id": "c", "name": "Ch 1"}]},
    })
    out = await client.get_audiobook(ID)
    assert out["authors"] == ["Au"] and out["chapters"][0]["name"] == "Ch 1"
    assert client._request.await_args.kwargs["params"] == {"market": "US"}


@pytest.mark.asyncio
async def test_search_accepts_podcast_and_audiobook_types(client):
    client._request = AsyncMock(return_value={"shows": {"items": [{"id": "s", "name": "Pod"}]}})
    out = await client.search_catalog("pod", "show", 3)
    assert out[0]["name"] == "Pod"
    with pytest.raises(ValueError):
        await client.search_catalog("x", "user")


# ---- library ----


@pytest.mark.asyncio
async def test_set_saved_chunks_at_40_and_uses_method(client):
    client._request = AsyncMock(return_value={})
    refs = [f"spotify:album:{ID}"] * 85
    assert await client.set_saved(refs, save=False) == 85
    calls = client._request.await_args_list
    assert len(calls) == 3
    assert all(c.args == ("DELETE", "/me/library") for c in calls)
    assert calls[0].kwargs["params"]["uris"].count("spotify:album:") == 40


@pytest.mark.asyncio
async def test_set_saved_rejects_bare_ids_before_calling_spotify(client):
    client._request = AsyncMock()
    with pytest.raises(ValueError):
        await client.set_saved([ID])
    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_saved_pairs_uris_with_flags(client):
    client._request = AsyncMock(return_value=[True, False])
    out = await client.check_saved([f"spotify:artist:{ID}", f"spotify:album:{ID}"])
    assert [o["saved"] for o in out] == [True, False]
    assert client._request.await_args.args == ("GET", "/me/library/contains")


@pytest.mark.asyncio
async def test_saved_items_unwraps_each_kind(client):
    client._request = AsyncMock(return_value={
        "items": [{"added_at": "t", "album": {"id": "a", "name": "Alb"}}], "next": None,
    })
    out = await client.get_saved("album")
    assert out == [{"added_at": "t", "id": "a", "uri": None, "name": "Alb", "type": None, "url": None}]
    assert client._request.await_args.args == ("GET", "/me/albums")
    with pytest.raises(ValueError):
        await client.get_saved("track")


@pytest.mark.asyncio
async def test_top_items_validates_and_calls_endpoint(client):
    client._request = AsyncMock(return_value={"items": [{"id": "a", "name": "A"}]})
    await client.get_top_items("artists", "short_term", 99)
    assert client._request.await_args.args == ("GET", "/me/top/artists")
    assert client._request.await_args.kwargs["params"] == {"time_range": "short_term", "limit": 50}
    with pytest.raises(ValueError):
        await client.get_top_items("albums")
    with pytest.raises(ValueError):
        await client.get_top_items("tracks", "forever")


# ---- playback ----


@pytest.mark.asyncio
async def test_play_context_and_tracks_bodies(client):
    client._request = AsyncMock(return_value={})
    await client.start_playback(context=f"https://open.spotify.com/album/{ID}")
    assert client._request.await_args.kwargs["json_body"] == {"context_uri": f"spotify:album:{ID}"}
    await client.start_playback(tracks=[ID, f"spotify:track:{ID}"])
    assert client._request.await_args.kwargs["json_body"] == {"uris": [f"spotify:track:{ID}"] * 2}
    await client.start_playback()
    assert client._request.await_args.kwargs["json_body"] is None


@pytest.mark.asyncio
async def test_play_rejects_conflicting_or_unsupported_targets(client):
    client._request = AsyncMock()
    with pytest.raises(ValueError):
        await client.start_playback(track=ID, context=f"spotify:album:{ID}")
    with pytest.raises(ValueError):
        await client.start_playback(context=f"spotify:track:{ID}")
    with pytest.raises(ValueError):
        await client.start_playback(context=ID)
    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_controls_shape_requests(client):
    client._request = AsyncMock(return_value={})
    await client.seek(30000)
    await client.set_shuffle(True)
    await client.set_repeat("context")
    await client.transfer_playback("dev1", play=True)
    calls = [(c.args, c.kwargs) for c in client._request.await_args_list]
    assert calls[0] == (("PUT", "/me/player/seek"), {"params": {"position_ms": 30000}})
    assert calls[1] == (("PUT", "/me/player/shuffle"), {"params": {"state": "true"}})
    assert calls[2] == (("PUT", "/me/player/repeat"), {"params": {"state": "context"}})
    assert calls[3] == (("PUT", "/me/player"), {"json_body": {"device_ids": ["dev1"], "play": True}})


@pytest.mark.asyncio
async def test_invalid_player_inputs_never_call_spotify(client):
    client._request = AsyncMock()
    with pytest.raises(ValueError):
        await client.seek(-1)
    with pytest.raises(ValueError):
        await client.set_repeat("loop")
    with pytest.raises(ValueError):
        await client.transfer_playback(" ")
    client._request.assert_not_awaited()


# ---- playlist contents ----


@pytest.mark.asyncio
async def test_reorder_sends_range_and_returns_snapshot(client):
    client._request = AsyncMock(return_value={"snapshot_id": "snap"})
    assert await client.reorder_playlist_items("pl", 9, 0, 2) == "snap"
    assert client._request.await_args.args == ("PUT", "/playlists/pl/items")
    assert client._request.await_args.kwargs["json_body"] == {
        "range_start": 9, "insert_before": 0, "range_length": 2,
    }
    with pytest.raises(ValueError):
        await client.reorder_playlist_items("pl", -1, 0)


@pytest.mark.asyncio
async def test_replace_puts_first_100_then_appends_rest(client):
    client._request = AsyncMock(return_value={})
    uris = [f"spotify:track:{i:022d}" for i in range(130)]
    assert await client.replace_playlist_items("pl", uris) == 130
    put, post = client._request.await_args_list
    assert put.args == ("PUT", "/playlists/pl/items") and len(put.kwargs["json_body"]["uris"]) == 100
    assert post.args == ("POST", "/playlists/pl/items") and len(post.kwargs["json_body"]["uris"]) == 30


@pytest.mark.asyncio
async def test_add_tracks_at_position_keeps_order_across_chunks(client):
    client._request = AsyncMock(return_value={})
    uris = [f"spotify:track:{i:022d}" for i in range(150)]
    await client.add_tracks_at("pl", uris, position=5)
    bodies = [c.kwargs["json_body"] for c in client._request.await_args_list]
    assert [b["position"] for b in bodies] == [5, 105]


# ---- server wiring ----


def _fn(tool):
    return getattr(tool, "fn", tool)


@pytest.mark.asyncio
async def test_replace_refuses_when_any_track_is_unrecognized(monkeypatch):
    fake = AsyncMock()
    fake.resolve_playlist.return_value = {"id": "pl", "name": "P", "url": "u"}
    fake.parse_track_ref = SpotifyClient.parse_track_ref
    monkeypatch.setattr(server, "spotify", fake)
    out = json.loads(await _fn(server.replace_playlist_items)("P", [ID, "garbage"]))
    assert out["unresolved"] == ["garbage"]
    fake.replace_playlist_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_shapes_value_errors_as_json(monkeypatch):
    fake = AsyncMock()
    fake.set_saved.side_effect = ValueError("Not a recognizable Spotify URI")
    monkeypatch.setattr(server, "spotify", fake)
    out = json.loads(await _fn(server.save_to_library)(["nope"]))
    assert "Not a recognizable" in out["error"]


@pytest.mark.asyncio
async def test_tool_shapes_spotify_errors_as_json(monkeypatch):
    fake = AsyncMock()
    fake.get_top_items.side_effect = SpotifyError("Spotify API GET /me/top/tracks failed (403)")
    monkeypatch.setattr(server, "spotify", fake)
    out = json.loads(await _fn(server.top_items)())
    assert "403" in out["error"]
