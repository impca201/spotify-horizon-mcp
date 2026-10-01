# Spotify Horizon metadata

Use these values in the existing Horizon server's metadata fields. This file documents the settings; Horizon does not automatically import it.

| Field | Value |
| --- | --- |
| Title | Spotify |
| Description | Music and podcast streaming service. |
| Entrypoint | `server.py:mcp` |
| Branch | `main` |
| Authentication | Enabled |

Keep the existing server URL and environment variables. After deployment, verify tool discovery and a read-only call before testing changes to account data or devices.
