# unreal-mcp-bridge

A tiny stdio↔HTTP relay that keeps **Claude Code** connected to Unreal Engine's
built-in **Model Context Protocol** server across editor restarts — so you never
have to run `/mcp reconnect` again.

## The problem

UE 5.8+ ships an MCP server (`Engine/Plugins/Experimental/ModelContextProtocol`)
that Claude Code talks to over HTTP. It's spec-compliant: every request after the
handshake must carry an `Mcp-Session-Id` naming a session that lives in the
editor's memory. Restart the editor and that session is gone, so the server
correctly rejects the now-stale id:

```
HTTP 404  "Unknown session id '...'; client should reinitialize"
```

A spec-compliant client is supposed to react to that 404 by starting a fresh
session via `initialize`. Claude Code's HTTP transport doesn't — it surfaces the
error and waits for a manual `/mcp reconnect`. The server is fine; the client
just doesn't self-heal.

## What the bridge does

Claude Code spawns this script as an ordinary **stdio** MCP server. Stdio has no
session ids and no reconnect concept, so from Claude's side the server is a local
process that never goes away. The bridge owns the flaky upstream HTTP session on
Claude's behalf:

- forwards JSON-RPC both directions, attaching the live `Mcp-Session-Id` upstream;
- on a `404` / unknown-session (or a dropped connection), silently re-runs the
  full handshake (`initialize` + `notifications/initialized`) and **replays the
  request that failed** — the tool call just succeeds, a moment late;
- if the editor isn't up yet (connection refused), **holds the request and
  retries with backoff**, so you can start Claude before UE;
- after a reconnect, emits `notifications/tools/list_changed` downstream, so a
  recompiled or changed tool set is picked up automatically.

It's pure Python standard library — no `pip install`, no MCP SDK. It relays
opaque JSON-RPC envelopes rather than modelling tools, so it keeps working
unchanged when the upstream tool set changes (e.g. when BoundHound adds tools).

## Setup

Point your Claude Code MCP config at the bridge instead of the raw endpoint. In
`.mcp.json`:

```json
{
  "mcpServers": {
    "UnrealMCP": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/unreal-mcp-bridge/mcp_bridge.py"],
      "env": {}
    }
  }
}
```

Restart Claude Code so it re-spawns the server, and you're done. Verify the
connection with `/mcp` — `UnrealMCP` should list the editor's tools.

### Reverting

The bridge is a drop-in; to go back to talking to UE directly, restore the
original config:

```json
{
  "mcpServers": {
    "UnrealMCP": { "type": "http", "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

## Configuration

All optional, via environment variables (set them in the `env` block above):

| Variable | Default | Meaning |
| --- | --- | --- |
| `UNREAL_MCP_URL` | `http://127.0.0.1:8000/mcp` | Upstream MCP endpoint. |
| `UNREAL_MCP_TOOL_TIMEOUT` | `600` | Socket timeout (s) for `tools/call`. |
| `UNREAL_MCP_QUICK_TIMEOUT` | `30` | Socket timeout (s) for handshake / list. |

## Diagnostics

The bridge logs to **stderr**, which Claude Code captures as MCP server output.
Expect lines like:

```
[mcp-bridge] bridge up; upstream = http://127.0.0.1:8000/mcp
[mcp-bridge] downstream initialized (epoch 1)
[mcp-bridge] upstream session invalid (HTTP 404); reinitializing   <- editor was restarted
[mcp-bridge] established upstream session (epoch 2)
```

## Requirements

- Python 3.10+ (uses `X | Y` type hints).
- Claude Code (or any MCP client that spawns stdio servers).
- An Unreal Engine editor running the built-in MCP server.

## License

MIT.
