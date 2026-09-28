# Spotify MCP for Prefect Horizon

A personal Spotify MCP server for finding artists and managing playlists. This Horizon-ready fork is based on [pete-builds/mcp-spotify](https://github.com/pete-builds/mcp-spotify), licensed under MIT. It uses FastMCP and the Spotify Web API.

Tools: `search_artist`, `get_artist_top_tracks`, `create_playlist_from_artists`, `add_artists_to_playlist`, `create_playlist_from_tracks`, `list_my_playlists`, `get_playlist_metadata`, `list_playlist_tracks`, `update_playlist`, `delete_playlist`, and `remove_tracks_from_playlist`. It does not control playback or read listening history.

## Spotify authorization

1. Create a Web API app at [Spotify for Developers](https://developer.spotify.com/dashboard). Development Mode requires the app owner to have Premium and allows up to five authorized users. Add your Spotify account under **User Management**, even if you own the app.
2. Add the exact redirect URI `http://127.0.0.1:8765/callback` to the Spotify app.
3. Run the one-time bootstrap **on your own computer**. It requests `playlist-read-private`, `playlist-modify-private`, and `playlist-modify-public`.

   ```bash
   export SPOTIFY_CLIENT_ID='your-client-id'
   export SPOTIFY_CLIENT_SECRET='your-client-secret'
   python3 bootstrap.py
   ```

4. Copy the refresh token displayed by the script into Horizon's secret environment variable field. Do not commit it or paste it into chat. Spotify refresh tokens require reauthorization after six months; rerun bootstrap and update the Horizon secret when needed.

## Deploy in Prefect Horizon

Connect `impca201/spotify-horizon-mcp` in Horizon and choose its `main` branch. Set the **server path / entrypoint** to `server.py` and the dependency file to `pyproject.toml`. Use Python 3.13 if Horizon asks for a runtime; the project supports Python 3.11 or newer.

Set these server environment variables in Horizon:

| Name | Value |
| --- | --- |
| `SPOTIFY_CLIENT_ID` | Spotify app Client ID |
| `SPOTIFY_CLIENT_SECRET` | Spotify app Client Secret (secret) |
| `SPOTIFY_REFRESH_TOKEN` | Token from `bootstrap.py` (secret) |

Horizon provides the HTTPS MCP endpoint and its own client authentication. Use Horizon's displayed URL and authentication setting when connecting an MCP client. Do not copy the local Docker port or set up a Spotify redirect URL on Horizon: Spotify authorization is completed locally during bootstrap. All tools act on one Spotify account, so limit Horizon access to people who may change that account's playlists.

Tool discovery works before the Spotify variables are set. Tool calls need all three variables. After deployment, list tools and call the read-only `search_artist` tool; verify the response has an artist ID.

## Local development

```bash
uv venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.lock
uv pip install --python .venv/bin/python -e '.[dev]'
uv run pytest
```

To run the local HTTP server with credentials in your environment, use `uv run python server.py`. The upstream Docker setup is retained for local use. Horizon imports the `mcp` instance in `server.py` directly.

## Limits and troubleshooting

- Spotify Development Mode restricts some endpoints and caps searches at ten results. The upstream client handles the playlist endpoint changes introduced in February 2026.
- A Spotify 401 usually means expired or revoked authorization. Repeat bootstrap and replace the refresh token in Horizon. A 403 can mean your Spotify user is absent from User Management or that an endpoint is unavailable in Development Mode.
- A build error involving `pete-mcp-core` points to the immutable GitHub tarball dependency in `pyproject.toml`; it is a build dependency, not a Spotify credential.
- The server keeps a rotated refresh token in memory while running. A cold restart after rotation may require a new bootstrap. A successful build alone does not prove a live Spotify API call.

## License and credit

MIT, with [the upstream license](LICENSE) preserved. Based on [Pete Stergion's mcp-spotify](https://github.com/pete-builds/mcp-spotify). The original tool implementations and tests are credited to the upstream project.
