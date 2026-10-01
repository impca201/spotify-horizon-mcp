# Spotify MCP for Prefect Horizon

A personal Spotify MCP server for searching and looking up music, podcasts and audiobooks, controlling playback, reading and editing your library, and managing playlists. This Horizon-ready fork is based on [pete-builds/mcp-spotify](https://github.com/pete-builds/mcp-spotify), licensed under MIT. It uses FastMCP and the Spotify Web API.

Tools, grouped by what they do:

- **Search and lookup:** `search_spotify` (tracks, albums, artists, playlists, shows, episodes, audiobooks), `search_artist`, `get_track`, `get_album`, `get_artist`, `get_artist_albums`, `get_show`, `get_show_episodes`, `get_episode`, `get_audiobook`, `get_artist_top_tracks`, `get_my_profile`.
- **Playback:** `current_playback`, `available_devices`, `play_music` (a track, several tracks, or an album, artist, playlist or show), `pause_music`, `skip_track`, `seek_playback`, `set_shuffle`, `set_repeat`, `set_playback_volume`, `transfer_playback`, `playback_queue`, `queue_track`.
- **Library and listening:** `liked_songs`, `set_song_liked`, `saved_items` (albums, shows, episodes, audiobooks), `check_saved`, `save_to_library`, `remove_from_library` (also follows and unfollows artists and playlists), `recently_played`, `top_items`, `followed_artists`.
- **Playlists:** `list_my_playlists`, `get_playlist_metadata`, `list_playlist_tracks`, `get_playlist_cover`, `create_playlist_from_artists`, `create_playlist_from_tracks`, `add_artists_to_playlist`, `add_tracks_to_playlist`, `reorder_playlist_items`, `replace_playlist_items`, `update_playlist`, `remove_tracks_from_playlist`, `delete_playlist`.

Playback needs Spotify Premium and an active Spotify Connect device.

## Spotify authorization

1. Create a Web API app at [Spotify for Developers](https://developer.spotify.com/dashboard). Development Mode requires the app owner to have Premium and allows up to five authorized users. Add your Spotify account under **User Management**, even if you own the app.
2. Add the exact redirect URI `http://127.0.0.1:8765/callback` to the Spotify app.
3. Run the one-time bootstrap **on your own computer**, from a local copy of this repository. It needs only Python 3; no project dependencies or Docker are needed for this step. It requests playlist read/write, playback read/write, recently played, library read/write, top items and follow read/write scopes. If you generated a refresh token with an earlier version, rerun bootstrap to grant the added scopes. Until you do, `top_items`, `followed_artists` and following artists through `save_to_library` return a 403.

   **Windows (PowerShell):** Clone [this repository](https://github.com/impca201/spotify-horizon-mcp) with GitHub Desktop, or use **Fetch origin / Pull origin** if you already cloned it. In GitHub Desktop, choose **Repository → Show in Explorer**. In File Explorer, click the address bar, type `powershell`, and press Enter. The prompt should show the repository folder (for example, `PS C:\Apps\GitHub\spotify-horizon-mcp>`).

   Run the following steps **one at a time**. When a command asks for input, enter the requested *value* and press Enter before copying the next command. Do not paste the entire block of commands at once: a `Read-Host` prompt may take the next command as your credential.

   First, check that Python works:

   ```powershell
   python --version
   ```

   If it prints a Python 3 version, continue. The `py` launcher is optional; if `py -3 --version` says the command is not recognized, use `python` as shown here.

   Enter **only this command**, then wait for the `Spotify Client ID:` prompt. Paste the Client ID from your Spotify Developer app and press Enter:

   ```powershell
   $env:SPOTIFY_CLIENT_ID = Read-Host "Spotify Client ID"
   ```

   Enter **only this command**, then wait for the `Spotify Client Secret:` prompt. Paste the Client Secret and press Enter. The secret does not appear while you type or paste it:

   ```powershell
   $secret = Read-Host "Spotify Client Secret" -AsSecureString
   ```

   Now run these commands, pressing Enter after each line:

   ```powershell
   $env:SPOTIFY_CLIENT_SECRET = [System.Net.NetworkCredential]::new("", $secret).Password
   python .\bootstrap.py
   ```

   The script opens a browser for Spotify authorization and briefly listens on `127.0.0.1:8765` for the redirect. Approve the requested scopes, then return to PowerShell for the refresh token. If it says `ERROR: set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in env`, repeat the commands above one at a time, waiting for each prompt; keep the same PowerShell window open. These environment variables exist only in that window. Do not paste credentials or the token into chat, screenshots, or a Git commit.

   On macOS/Linux, run these commands from the cloned repository:

   ```bash
   export SPOTIFY_CLIENT_ID='your-client-id'
   export SPOTIFY_CLIENT_SECRET='your-client-secret'
   python3 bootstrap.py
   ```

4. Copy the refresh token displayed by the script into Horizon's secret environment variable field. Do not commit it or paste it into chat. Spotify refresh tokens require reauthorization after six months; rerun bootstrap and update the Horizon secret when needed.

## Deploy in Prefect Horizon

Connect `impca201/spotify-horizon-mcp` in Horizon and choose its `main` branch. Set the **entrypoint** to `server.py:mcp`. Horizon detects dependencies from `pyproject.toml`. Use Python 3.13 if Horizon asks for a runtime; the project supports Python 3.11 or newer.

Set these server environment variables in Horizon:

| Name | Value |
| --- | --- |
| `SPOTIFY_CLIENT_ID` | Spotify app Client ID |
| `SPOTIFY_CLIENT_SECRET` | Spotify app Client Secret (secret) |
| `SPOTIFY_REFRESH_TOKEN` | Token from `bootstrap.py` (secret) |

Enable **Authentication** in Horizon. Horizon then provides an HTTPS MCP endpoint protected for your authorized users. Use its displayed URL when connecting an MCP client. Do not copy the local Docker port or set up a Spotify redirect URL on Horizon: Spotify authorization is completed locally during bootstrap. All tools act on one Spotify account, so limit Horizon access to people who may change that account's playlists.

Tool discovery works before the Spotify variables are set. Tool calls need all three variables. After deployment, list tools and call the read-only `search_spotify` tool with a track query; verify the response has a track ID and URI. Then call `current_playback` to check the user's scopes and active device.

## Local development

```bash
uv venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.lock
uv pip install --python .venv/bin/python -e '.[dev]'
uv run pytest
```

To run the local HTTP server with credentials in your environment, use `uv run python server.py`. The upstream Docker setup is retained for local use. Horizon imports the `mcp` instance in `server.py` directly.

## Limits and troubleshooting

- Spotify Development Mode restricts some endpoints and caps searches at ten results. Batch lookups (several tracks, albums or artists at once), artist top-tracks, related artists, new releases, browse categories, audio analysis and recommendations return 403/404 for Development Mode apps, so this server does not offer them.
- Audiobooks are only served in the US, UK, Canada, Ireland, New Zealand and Australia.
- This fork uses the newer `/me/library` and `/playlists/{id}/items` routes; check Spotify's current Web API documentation if an endpoint returns 403/404. Playback requires Premium and an active device.
- A Spotify 401 usually means expired or revoked authorization. Repeat bootstrap and replace the refresh token in Horizon. A 403 can mean your Spotify user is absent from User Management or that an endpoint is unavailable in Development Mode.
- A build error involving `pete-mcp-core` points to the immutable GitHub tarball dependency in `pyproject.toml`; it is a build dependency, not a Spotify credential.
- The server keeps a rotated refresh token in memory while running. A cold restart after rotation may require a new bootstrap. A successful build alone does not prove a live Spotify API call.

## License and credit

MIT, with [the upstream license](LICENSE) preserved. Based on [Pete Stergion's mcp-spotify](https://github.com/pete-builds/mcp-spotify). The original artist and playlist tools and tests are credited to the upstream project; this fork adds broader Spotify tools.
