"""MCP Spotify - create playlists from artist lists via the Spotify Web API.

Provides Claude Code tools to search for artists, pull their top tracks, and
assemble fresh playlists on Pete's Spotify account via the Model Context
Protocol (Streamable HTTP transport).

Uses OAuth 2.0 Authorization Code flow with a long-lived refresh token.
See bootstrap.py for the one-time token acquisition procedure.
"""

import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastmcp import FastMCP
from pete_mcp_core import (
    build_auth_provider,
    configure_logging,
    format_response,
    run_server,
    tool_errors,
)
from pete_mcp_core.settings import BaseCoreSettings
from pydantic import AliasChoices, Field, SecretStr, ValidationError

from clients.spotify import SpotifyClient, SpotifyError

load_dotenv()


class SpotifySettings(BaseCoreSettings):
    spotify_client_id: str = Field(
        default="",
        validation_alias=AliasChoices("SPOTIFY_CLIENT_ID", "MCP_SPOTIFY_CLIENT_ID"),
    )
    spotify_client_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "SPOTIFY_CLIENT_SECRET", "MCP_SPOTIFY_CLIENT_SECRET"
        ),
    )
    spotify_refresh_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "SPOTIFY_REFRESH_TOKEN", "MCP_SPOTIFY_REFRESH_TOKEN"
        ),
    )


try:
    settings = SpotifySettings()
except ValidationError as exc:
    raise RuntimeError(f"Invalid Spotify MCP configuration: {exc}") from exc

configure_logging(
    settings.log_level,
    settings.log_format,
    extra_sensitive_keys=["spotify_client_secret", "spotify_refresh_token"],
)
log = logging.getLogger("mcp-spotify")

missing = [
    name
    for name, value in (
        ("SPOTIFY_CLIENT_ID", settings.spotify_client_id),
        ("SPOTIFY_CLIENT_SECRET", settings.spotify_client_secret.get_secret_value()),
        ("SPOTIFY_REFRESH_TOKEN", settings.spotify_refresh_token.get_secret_value()),
    )
    if not value
]
if missing:
    # Horizon imports this module to inspect tools before secrets are configured.
    # SpotifyClient performs no network request until a tool runs.
    log.warning("Spotify credentials needed for tool calls: %s", ", ".join(missing))

# --- Initialize client ---
spotify = SpotifyClient(
    client_id=settings.spotify_client_id,
    client_secret=settings.spotify_client_secret.get_secret_value(),
    refresh_token=settings.spotify_refresh_token.get_secret_value(),
)


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        await spotify.close()


# --- MCP Server ---
mcp = FastMCP(
    "Spotify",
    lifespan=lifespan,
    auth=build_auth_provider(
        settings.auth_token,
        client_id="spotify",
        required=settings.auth_required,
        logger=log,
    ),
)

# Alias so existing `_format(...)` call sites stay unchanged.
_format = format_response

# Decorator applied to every @mcp.tool() below. The per-tool
# try/except SpotifyError blocks remain in place for now and take precedence;
# once we've validated this decorator in production, a follow-up commit will
# remove the redundant per-tool handlers.
_spotify_errors = tool_errors("mcp-spotify", catch=SpotifyError)

# --- Tool annotations ---
# Nothing in an MCP manifest distinguishes delete_playlist from search_artist
# unless the tool says so, so a client has no basis on which to prompt before a
# destructive call. Every tool here reaches Spotify over the network, so
# openWorldHint is True throughout.

#: Reads only. Safe to repeat, safe to call speculatively.
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: Sets a value on an existing playlist. Applying the same name, description,
#: or visibility twice lands in the same place.
WRITE_IDEMPOTENT = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: Creates a playlist, or appends to one. Not idempotent, deliberately:
#: Spotify permits duplicate tracks, and a repeated create makes a second
#: playlist with the same name. An idempotent hint would invite the retry that
#: goes wrong.
CREATE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}

#: Removes tracks, or unfollows the playlist entirely. Worth confirming.
DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}


def _interleave(groups: list[list[str]]) -> list[str]:
    """Round-robin interleave: [A, B, C, A, B, C, ...] so the same artist
    doesn't play back-to-back."""
    out: list[str] = []
    if not groups:
        return out
    max_len = max(len(g) for g in groups)
    for i in range(max_len):
        for g in groups:
            if i < len(g):
                out.append(g[i])
    return out


async def _resolve_track_refs(refs: list[str]) -> tuple[list[str], list[str]]:
    """Normalize a list of user-supplied track refs (URIs, URLs, IDs) to
    `spotify:track:<id>` URIs. Returns (resolved_uris, unresolved_inputs)."""
    resolved: list[str] = []
    bad: list[str] = []
    for r in refs:
        try:
            resolved.append(spotify.parse_track_ref(r))
        except ValueError:
            bad.append(r)
    return resolved, bad


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def search_artist(name: str, limit: int = 5) -> str:
    """Search Spotify for artists matching a name.

    Args:
        name: Artist name to search (free text, e.g. "Radiohead").
        limit: Max matches to return (1-50, default 5).

    Returns:
        JSON list of artists with id, name, popularity, genres, followers, url.
    """
    try:
        results = await spotify.search_artists(name, limit=limit)
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})
    return _format(results)


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_artist_top_tracks(artist_name: str, limit: int = 5, market: str = "US") -> str:
    """Get an artist's most popular tracks in a given market.

    Resolves the artist by name (prefers exact-match), then returns their
    tracks ordered by Spotify's relevance ranking. Under the hood this uses
    search-with-artist-filter rather than the `/top-tracks` endpoint, which is
    403-restricted for new Spotify Developer apps. Tracks typically cap around
    5-7 results regardless of limit due to the same restrictions.

    Args:
        artist_name: Artist name to look up (e.g. "Radiohead").
        limit: Max tracks to return (1-10, default 5).
        market: ISO 3166-1 alpha-2 country code (default "US").

    Returns:
        JSON list of tracks with id, name, uri, album, artists, url.
    """
    try:
        tracks = await spotify.get_top_tracks_for_artist(
            artist_name, limit=limit, market=market
        )
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})
    return _format(tracks)


async def _collect_tracks_by_artist(
    artists: list[str], tracks_per_artist: int, market: str
) -> tuple[list[list[str]], list[dict], list[str]]:
    """Fetch top tracks for each artist. Returns:
      * per_artist_uris: list of track-URI lists, one per resolved artist
      * resolved: metadata for each resolved artist
      * not_found: the input names that didn't resolve to any tracks
    """
    per_artist_uris: list[list[str]] = []
    resolved: list[dict] = []
    not_found: list[str] = []
    for artist_name in artists:
        top = await spotify.get_top_tracks_for_artist(
            artist_name, limit=tracks_per_artist, market=market
        )
        if not top:
            not_found.append(artist_name)
            continue
        per_artist_uris.append([t["uri"] for t in top])
        resolved.append(
            {
                "query": artist_name,
                "matched": top[0]["artists"][0],
                "tracks_added": len(top),
            }
        )
    return per_artist_uris, resolved, not_found


@mcp.tool(annotations=CREATE)
@_spotify_errors
async def create_playlist_from_artists(
    artists: list[str],
    playlist_name: str,
    tracks_per_artist: int = 5,
    public: bool = False,
    description: str = "",
    market: str = "US",
    shuffle: bool = True,
) -> str:
    """Create a new Spotify playlist populated with top tracks from each artist.

    Resolves each artist name to its Spotify ID (best match), pulls their top
    tracks in the given market, and adds them to a new playlist on Pete's
    account. Artists that cannot be resolved are returned in artists_not_found.

    Args:
        artists: List of artist names (e.g. ["Radiohead", "Talking Heads"]).
        playlist_name: Name for the new playlist.
        tracks_per_artist: How many top tracks per artist to add (1-10, default 5).
            Note: Spotify's search API may return fewer than requested in
            Development Mode (typically 5-7 max per artist).
        public: Whether the playlist is public (default False).
        description: Optional playlist description.
        market: ISO 3166-1 alpha-2 country code for top-tracks lookup (default "US").
        shuffle: If True (default), interleave by artist so the same artist
            doesn't play back-to-back. If False, group all of artist A, then B, etc.

    Returns:
        JSON with playlist_id, url, name, track_count, artists_resolved, artists_not_found.
    """
    tracks_per_artist = max(1, min(tracks_per_artist, 10))
    if not artists:
        return _format({"error": "artists list is empty"})

    try:
        per_artist, resolved, not_found = await _collect_tracks_by_artist(
            artists, tracks_per_artist, market
        )
        if not per_artist:
            return _format(
                {"error": "No tracks resolved from any artist",
                 "artists_not_found": not_found}
            )

        if shuffle:
            track_uris = _interleave(per_artist)
        else:
            track_uris = [uri for group in per_artist for uri in group]

        playlist = await spotify.create_playlist(
            name=playlist_name, public=public, description=description
        )
        added = await spotify.add_tracks(playlist["id"], track_uris)

        return _format(
            {
                "playlist_id": playlist["id"],
                "url": playlist["url"],
                "name": playlist["name"],
                "track_count": added,
                "artists_resolved": resolved,
                "artists_not_found": not_found,
            }
        )
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=CREATE)
@_spotify_errors
async def add_artists_to_playlist(
    playlist: str,
    artists: list[str],
    tracks_per_artist: int = 5,
    market: str = "US",
    shuffle: bool = True,
) -> str:
    """Add top tracks from one or more artists to an EXISTING playlist.

    Use this when you want to expand a playlist you already have rather than
    start a fresh one. Same track-selection behavior as
    create_playlist_from_artists.

    Args:
        playlist: Identifier for the target playlist — accepts a full
            https://open.spotify.com/playlist/... URL, a spotify:playlist:...
            URI, the 22-char playlist ID, or a case-insensitive playlist name
            match against playlists you own or follow.
        artists: List of artist names.
        tracks_per_artist: How many top tracks per artist to add (1-10, default 5).
        market: ISO 3166-1 alpha-2 country code (default "US").
        shuffle: If True (default), interleave artists instead of grouping.

    Returns:
        JSON with playlist_id, url, name, added_count, artists_resolved,
        artists_not_found.
    """
    tracks_per_artist = max(1, min(tracks_per_artist, 10))
    if not artists:
        return _format({"error": "artists list is empty"})

    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format(
                {"error": f"Could not resolve playlist reference: {playlist!r}. "
                          "Pass a URL, URI, ID, or exact playlist name you own."}
            )
        per_artist, resolved, not_found = await _collect_tracks_by_artist(
            artists, tracks_per_artist, market
        )
        if not per_artist:
            return _format(
                {"error": "No tracks resolved from any artist",
                 "artists_not_found": not_found}
            )
        uris = _interleave(per_artist) if shuffle else [
            u for g in per_artist for u in g
        ]
        added = await spotify.add_tracks(target["id"], uris)
        return _format(
            {
                "playlist_id": target["id"],
                "url": target["url"],
                "name": target["name"],
                "added_count": added,
                "artists_resolved": resolved,
                "artists_not_found": not_found,
            }
        )
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=CREATE)
@_spotify_errors
async def create_playlist_from_tracks(
    tracks: list[str],
    playlist_name: str,
    public: bool = False,
    description: str = "",
) -> str:
    """Create a new playlist from specific tracks you already have in mind.

    Unlike create_playlist_from_artists, this skips the "top tracks" guess —
    you supply the exact tracks via Spotify URI, URL, or 22-char track ID.

    Args:
        tracks: List of track references. Each may be:
            - `spotify:track:<id>` URI
            - https://open.spotify.com/track/<id> URL (query params are OK)
            - bare 22-char track ID
        playlist_name: Name for the new playlist.
        public: Whether the playlist is public (default False).
        description: Optional playlist description.

    Returns:
        JSON with playlist_id, url, name, track_count, and unresolved (any
        inputs that couldn't be parsed as a track reference).
    """
    if not tracks:
        return _format({"error": "tracks list is empty"})
    uris, unresolved = await _resolve_track_refs(tracks)
    if not uris:
        return _format({"error": "No valid track references", "unresolved": unresolved})
    try:
        playlist = await spotify.create_playlist(
            name=playlist_name, public=public, description=description
        )
        added = await spotify.add_tracks(playlist["id"], uris)
        return _format(
            {
                "playlist_id": playlist["id"],
                "url": playlist["url"],
                "name": playlist["name"],
                "track_count": added,
                "unresolved": unresolved,
            }
        )
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def list_my_playlists(limit: int = 50) -> str:
    """List the authenticated user's playlists (owned + followed).

    Args:
        limit: Max playlists to return (1-50, default 50). Use this to find a
            playlist's ID or URL before calling add_artists_to_playlist etc.

    Returns:
        JSON list with id, name, url, track_count, public, owner_id,
        owner_name per playlist.
    """
    try:
        return _format(await spotify.get_my_playlists(limit=max(1, min(limit, 50))))
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_playlist_metadata(playlist: str) -> str:
    """Get header-only metadata for a playlist (no track listing).

    Cheap call for diff planning: snapshot_id changes whenever the playlist's
    contents change, so the sync engine can skip unchanged playlists before
    pulling the full track list.

    Args:
        playlist: URL, URI, 22-char ID, or exact name of the playlist.

    Returns:
        JSON with id, name, description, url, public, collaborative,
        snapshot_id, owner_id, owner_name, track_count.
    """
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        return _format(await spotify.get_playlist_metadata(target["id"]))
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def list_playlist_tracks(playlist: str) -> str:
    """List every track on a playlist with ISRC, artists, and duration.

    Returns one record per track. ISRC is included (from external_ids.isrc)
    and is the primary cross-service matching key for Spotify <-> Tidal sync.
    Local (non-Spotify) tracks added from a user's machine are skipped.

    Args:
        playlist: URL, URI, 22-char ID, or exact name of the playlist.

    Returns:
        JSON list with id, uri, name, artists, album, isrc, duration_ms,
        added_at per track.
    """
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        return _format(await spotify.get_playlist_tracks(target["id"]))
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def update_playlist(
    playlist: str,
    new_name: str = "",
    description: str = "",
    public: bool | None = None,
    collaborative: bool | None = None,
) -> str:
    """Rename a playlist, edit its description, or toggle public/private/collaborative.

    Leave any argument unset (or empty string) to keep its current value.

    Args:
        playlist: URL, URI, 22-char ID, or exact name of the playlist.
        new_name: New playlist name (leave empty to keep current).
        description: New description (leave empty to keep current).
        public: Toggle public flag (leave None to keep current).
        collaborative: Let others edit it (leave None to keep current; only
            allowed on a non-public playlist).

    Returns:
        JSON confirming the update with playlist_id, url, applied changes.
    """
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        await spotify.update_playlist(
            target["id"],
            name=new_name or None,
            description=description or None,
            public=public,
            collaborative=collaborative,
        )
        return _format(
            {
                "playlist_id": target["id"],
                "url": target["url"],
                "applied": {
                    k: v for k, v in [
                        ("name", new_name or None),
                        ("description", description or None),
                        ("public", public),
                        ("collaborative", collaborative),
                    ] if v is not None
                },
            }
        )
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=DESTRUCTIVE)
@_spotify_errors
async def delete_playlist(playlist: str) -> str:
    """Remove a playlist from your library. This is Spotify's "delete" —
    behind the scenes it unfollows the playlist; the underlying data persists
    on Spotify but it vanishes from your account.

    Args:
        playlist: URL, URI, 22-char ID, or exact name.

    Returns:
        JSON confirming removal.
    """
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        await spotify.unfollow_playlist(target["id"])
        return _format({"removed": True, "playlist_id": target["id"], "name": target["name"]})
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=DESTRUCTIVE)
@_spotify_errors
async def remove_tracks_from_playlist(playlist: str, tracks: list[str]) -> str:
    """Remove specific tracks from a playlist.

    Args:
        playlist: URL, URI, 22-char ID, or exact name.
        tracks: Track references — URIs, URLs, or 22-char track IDs.

    Returns:
        JSON with playlist_id, removed_count, unresolved.
    """
    if not tracks:
        return _format({"error": "tracks list is empty"})
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        uris, unresolved = await _resolve_track_refs(tracks)
        if not uris:
            return _format({"error": "No valid track references", "unresolved": unresolved})
        removed = await spotify.remove_tracks(target["id"], uris)
        return _format(
            {
                "playlist_id": target["id"],
                "name": target["name"],
                "removed_count": removed,
                "unresolved": unresolved,
            }
        )
    except SpotifyError as e:
        log.error("Spotify API error: %s", e)
        return _format({"error": str(e)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def search_spotify(query: str, item_type: str = "track", limit: int = 10) -> str:
    """Search Spotify. item_type: track, album, artist, playlist, show, episode or audiobook. Limit is 1–10."""
    try:
        return _format(await spotify.search_catalog(query, item_type, limit))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def current_playback() -> str:
    """Get the current track, device and playback state."""
    try:
        return _format(await spotify.get_playback_state())
    except SpotifyError as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def available_devices() -> str:
    """List Spotify Connect playback devices."""
    try:
        return _format(await spotify.get_devices())
    except SpotifyError as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def play_music(
    track: str | None = None,
    device_id: str | None = None,
    context: str | None = None,
    tracks: list[str] | None = None,
) -> str:
    """Start or resume playback. Optionally select a device.

    With no arguments, resumes. Otherwise give ONE of:
        track: a single track URI, URL or ID.
        tracks: several tracks (URIs, URLs or IDs), played in order.
        context: an album, artist, playlist or show as a spotify: URI or
            open.spotify.com URL (a bare ID is ambiguous and is rejected).
    """
    try:
        await spotify.start_playback(track, device_id, context=context, tracks=tracks)
        return _format({"ok": True, "action": "play"})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def pause_music() -> str:
    """Pause playback on the active Spotify device."""
    try:
        await spotify.playback_action("pause")
        return _format({"ok": True, "action": "pause"})
    except SpotifyError as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=CREATE)
@_spotify_errors
async def skip_track(direction: str = "next") -> str:
    """Skip to the next or previous track. Direction: next or previous."""
    try:
        await spotify.playback_action(direction)
        return _format({"ok": True, "action": direction})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def set_playback_volume(volume_percent: int) -> str:
    """Set playback volume between 0 and 100 percent."""
    try:
        await spotify.set_volume(volume_percent)
        return _format({"ok": True, "volume_percent": volume_percent})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def playback_queue() -> str:
    """Read the currently playing track and upcoming queue."""
    try:
        return _format(await spotify.get_queue())
    except SpotifyError as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=CREATE)
@_spotify_errors
async def queue_track(track: str, device_id: str | None = None) -> str:
    """Add a track URI, URL or ID to the playback queue."""
    try:
        await spotify.add_to_queue(track, device_id)
        return _format({"ok": True, "action": "queue"})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def liked_songs(limit: int = 20) -> str:
    """Read up to 50 saved Spotify tracks."""
    try:
        return _format(await spotify.get_saved_tracks(limit))
    except SpotifyError as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def set_song_liked(track: str, liked: bool = True) -> str:
    """Save or remove a track from Your Music using its Spotify URI, URL or ID."""
    try:
        await spotify.save_track(track, liked)
        return _format({"ok": True, "liked": liked})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def recently_played(limit: int = 20) -> str:
    """Read up to 50 recently played Spotify tracks."""
    try:
        return _format(await spotify.get_recently_played(limit))
    except SpotifyError as exc:
        return _format({"error": str(exc)})


# --- Catalog lookups ---


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_my_profile() -> str:
    """Your Spotify profile: id, display name, country and plan."""
    try:
        return _format(await spotify.get_profile())
    except SpotifyError as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_track(track: str, market: str | None = None) -> str:
    """Details for one track. Accepts a URI, URL or 22-char ID."""
    try:
        return _format(await spotify.get_track(track, market))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_album(album: str, market: str | None = None) -> str:
    """Details for one album, including its full track listing."""
    try:
        return _format(await spotify.get_album(album, market))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_artist(artist: str) -> str:
    """Details for one artist by URI, URL or ID. Popularity, genres and
    followers may be missing in Spotify's Development Mode."""
    try:
        return _format(await spotify.get_artist(artist))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_artist_albums(
    artist: str,
    include_groups: str = "album,single",
    limit: int = 20,
    market: str | None = None,
) -> str:
    """List an artist's releases.

    Args:
        artist: Artist URI, URL or ID (use search_artist to find it).
        include_groups: Comma-separated: album, single, appears_on, compilation.
        limit: Max releases to return (1-200, default 20).
        market: Optional ISO 3166-1 country code.
    """
    try:
        return _format(
            await spotify.get_artist_albums(artist, include_groups, limit, market)
        )
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_show(show: str, market: str | None = None) -> str:
    """Details for one podcast (show). Accepts a URI, URL or ID."""
    try:
        return _format(await spotify.get_show(show, market))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_show_episodes(
    show: str, limit: int = 20, market: str | None = None
) -> str:
    """List a podcast's episodes, newest first (limit 1-200, default 20)."""
    try:
        return _format(await spotify.get_show_episodes(show, limit, market))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_episode(episode: str, market: str | None = None) -> str:
    """Details for one podcast episode."""
    try:
        return _format(await spotify.get_episode(episode, market))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_audiobook(audiobook: str, market: str = "US") -> str:
    """Details and chapters for one audiobook. Spotify serves audiobooks only
    in the US, UK, Canada, Ireland, New Zealand and Australia, so market
    defaults to US."""
    try:
        return _format(await spotify.get_audiobook(audiobook, market))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def get_playlist_cover(playlist: str) -> str:
    """Cover image URLs (largest first) for a playlist. The URLs expire."""
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        return _format(await spotify.get_playlist_cover(target["id"]))
    except SpotifyError as exc:
        return _format({"error": str(exc)})


# --- Your library and listening ---


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def saved_items(item_type: str = "album", limit: int = 20) -> str:
    """Read what you saved in your library, newest first.

    Args:
        item_type: album, show, episode or audiobook. (Saved tracks are in
            liked_songs, followed artists in followed_artists.)
        limit: Max items (1-200, default 20).
    """
    try:
        return _format(await spotify.get_saved(item_type, limit))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def check_saved(items: list[str]) -> str:
    """Check whether items are in your library or followed.

    Args:
        items: Spotify URIs or open.spotify.com URLs of tracks, albums, artists,
            shows, episodes, audiobooks or playlists. Bare IDs are rejected
            because they do not say what kind of item they are.

    Returns:
        JSON list of {uri, saved}.
    """
    if not items:
        return _format({"error": "items list is empty"})
    try:
        return _format(await spotify.check_saved(items))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def save_to_library(items: list[str]) -> str:
    """Save items to your library, or follow artists and playlists.

    Args:
        items: Spotify URIs or open.spotify.com URLs of tracks, albums, artists,
            shows, episodes, audiobooks or playlists.
    """
    if not items:
        return _format({"error": "items list is empty"})
    try:
        return _format({"ok": True, "saved": await spotify.set_saved(items, True)})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=DESTRUCTIVE)
@_spotify_errors
async def remove_from_library(items: list[str]) -> str:
    """Remove items from your library, or unfollow artists and playlists.

    Args:
        items: Spotify URIs or open.spotify.com URLs of tracks, albums, artists,
            shows, episodes, audiobooks or playlists.
    """
    if not items:
        return _format({"error": "items list is empty"})
    try:
        return _format({"ok": True, "removed": await spotify.set_saved(items, False)})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def top_items(
    item_type: str = "tracks", time_range: str = "medium_term", limit: int = 20
) -> str:
    """Your most listened-to artists or tracks. Needs the user-top-read scope.

    Args:
        item_type: artists or tracks.
        time_range: short_term (about 4 weeks), medium_term (about 6 months) or
            long_term (about a year).
        limit: Max items (1-50, default 20).
    """
    try:
        return _format(await spotify.get_top_items(item_type, time_range, limit))
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=READ_ONLY)
@_spotify_errors
async def followed_artists(limit: int = 20) -> str:
    """Artists you follow (limit 1-50, default 20). Needs the user-follow-read scope."""
    try:
        return _format(await spotify.get_followed_artists(limit))
    except SpotifyError as exc:
        return _format({"error": str(exc)})


# --- Playback controls ---


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def seek_playback(position_ms: int) -> str:
    """Jump to a position in the current track, in milliseconds."""
    try:
        await spotify.seek(position_ms)
        return _format({"ok": True, "position_ms": position_ms})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def set_shuffle(enabled: bool) -> str:
    """Turn shuffle on or off."""
    try:
        await spotify.set_shuffle(enabled)
        return _format({"ok": True, "shuffle": enabled})
    except SpotifyError as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def set_repeat(mode: str) -> str:
    """Set repeat mode: track, context (album or playlist) or off."""
    try:
        await spotify.set_repeat(mode)
        return _format({"ok": True, "repeat": mode})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=WRITE_IDEMPOTENT)
@_spotify_errors
async def transfer_playback(device_id: str, play: bool = False) -> str:
    """Move playback to another device (get its id from available_devices).

    Args:
        device_id: Target device id.
        play: Start playing on the new device (default keeps the current state).
    """
    try:
        await spotify.transfer_playback(device_id, play)
        return _format({"ok": True, "device_id": device_id, "play": play})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


# --- Playlist contents ---


@mcp.tool(annotations=CREATE)
@_spotify_errors
async def add_tracks_to_playlist(
    playlist: str, tracks: list[str], position: int | None = None
) -> str:
    """Add specific tracks to an EXISTING playlist.

    Args:
        playlist: URL, URI, 22-char ID, or exact name of the playlist.
        tracks: Track URIs, URLs or 22-char IDs.
        position: Zero-based insert position (default: append at the end).
    """
    if not tracks:
        return _format({"error": "tracks list is empty"})
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        uris, unresolved = await _resolve_track_refs(tracks)
        if not uris:
            return _format({"error": "No valid track references", "unresolved": unresolved})
        added = await spotify.add_tracks_at(target["id"], uris, position)
        return _format(
            {
                "playlist_id": target["id"],
                "name": target["name"],
                "added_count": added,
                "unresolved": unresolved,
            }
        )
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=CREATE)
@_spotify_errors
async def reorder_playlist_items(
    playlist: str, range_start: int, insert_before: int, range_length: int = 1
) -> str:
    """Move a block of tracks within a playlist. Positions are zero-based.

    Example: move the last of 10 tracks to the top with range_start=9,
    insert_before=0. Not idempotent: repeating it moves the block again.
    """
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        snapshot = await spotify.reorder_playlist_items(
            target["id"], range_start, insert_before, range_length
        )
        return _format({"playlist_id": target["id"], "snapshot_id": snapshot})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


@mcp.tool(annotations=DESTRUCTIVE)
@_spotify_errors
async def replace_playlist_items(playlist: str, tracks: list[str]) -> str:
    """Replace ALL tracks on a playlist with the given list. The old contents
    are lost, so confirm with the user first.

    Args:
        playlist: URL, URI, 22-char ID, or exact name of the playlist.
        tracks: Track URIs, URLs or 22-char IDs. An empty list clears the playlist.
    """
    try:
        target = await spotify.resolve_playlist(playlist)
        if not target:
            return _format({"error": f"Could not resolve playlist: {playlist!r}"})
        uris, unresolved = await _resolve_track_refs(tracks)
        if unresolved:
            # Refuse rather than silently drop part of the new contents after
            # the old ones are already gone.
            return _format({"error": "Unrecognized track references", "unresolved": unresolved})
        count = await spotify.replace_playlist_items(target["id"], uris)
        return _format({"playlist_id": target["id"], "name": target["name"], "track_count": count})
    except (SpotifyError, ValueError) as exc:
        return _format({"error": str(exc)})


def main() -> None:
    run_server(mcp, default_port=3703, default_transport="streamable-http")


if __name__ == "__main__":
    main()
