"""Every tool declares what it does to the user's Spotify account.

Nothing in an MCP manifest distinguishes delete_playlist from search_artist
unless the tool says so, so a client has no basis on which to prompt before a
destructive call.

Env vars are set before importing server, as tests/test_tools.py does, because
server.py validates them at import.
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("SPOTIFY_CLIENT_ID", "test-id")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test-secret")
os.environ.setdefault("SPOTIFY_REFRESH_TOKEN", "test-refresh")

import server

#: Removes tracks, or unfollows the playlist entirely.
MUST_BE_DESTRUCTIVE = {
    "delete_playlist",
    "remove_tracks_from_playlist",
    "remove_from_library",
    "replace_playlist_items",
}

#: Changes the account. A read-only hint on any of these is worse than no hint
#: at all: it tells a client not to bother asking.
MUST_NOT_BE_READ_ONLY = MUST_BE_DESTRUCTIVE | {
    "create_playlist_from_artists",
    "create_playlist_from_tracks",
    "add_artists_to_playlist",
    "update_playlist",
    "save_to_library",
    "add_tracks_to_playlist",
    "reorder_playlist_items",
    "seek_playback",
    "set_shuffle",
    "set_repeat",
    "transfer_playback",
}


@pytest.fixture(scope="module")
def tools():
    """The live manifest, not the source. What a client would receive."""
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def test_every_tool_is_annotated(tools):
    assert [name for name, t in tools.items() if t.annotations is None] == []


def test_removals_are_marked_destructive(tools):
    for name in MUST_BE_DESTRUCTIVE:
        assert name in tools, f"{name} vanished; update this list deliberately"
        assert tools[name].annotations.destructiveHint is True, name


def test_writes_are_never_marked_read_only(tools):
    mislabelled = [n for n in MUST_NOT_BE_READ_ONLY if tools[n].annotations.readOnlyHint]
    assert mislabelled == []


def test_creation_is_not_idempotent(tools):
    """Spotify permits duplicate tracks, and a repeated create makes a second playlist.

    Neither converges on the same state, so an idempotent hint would invite
    exactly the retry that goes wrong.
    """
    for name in ("create_playlist_from_artists", "create_playlist_from_tracks",
                 "add_artists_to_playlist", "add_tracks_to_playlist",
                 "reorder_playlist_items"):
        assert tools[name].annotations.idempotentHint is False, name


def test_update_playlist_is_idempotent(tools):
    """The other direction: setting the same name twice IS the same state.

    Marking it non-idempotent alongside the creates would be a false alarm, and
    hints that cry wolf get ignored.
    """
    assert tools["update_playlist"].annotations.idempotentHint is True


def test_no_tool_is_both_read_only_and_destructive(tools):
    contradictory = [
        name for name, t in tools.items()
        if t.annotations.readOnlyHint and t.annotations.destructiveHint
    ]
    assert contradictory == []


def test_every_tool_declares_an_open_world(tools):
    closed = [n for n, t in tools.items() if t.annotations.openWorldHint is not True]
    assert closed == []
