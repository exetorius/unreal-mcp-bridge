#!/usr/bin/env python3
"""
unreal-mcp-bridge — a stdio<->HTTP relay that keeps Claude Code connected to
Unreal Engine's built-in Model Context Protocol server across editor restarts.

The problem
-----------
UE's MCP server (Engine/Plugins/Experimental/ModelContextProtocol) is a
spec-compliant Streamable-HTTP server. Every post-initialize request must carry
an `Mcp-Session-Id` naming a session that lives in the editor's memory. When you
restart the editor, that session is gone, and the server correctly answers a
stale id with:

    HTTP 404  "Unknown session id '...'; client should reinitialize"

A spec-compliant client is supposed to treat that 404 as "start a new session
via initialize". Claude Code's HTTP transport doesn't — it surfaces the error
and waits for a manual `/mcp reconnect`.

What this bridge does
---------------------
Claude Code spawns this script as a plain stdio MCP server, so from Claude's
point of view the server is a local process that never has a session and never
goes away. The bridge owns the flaky upstream HTTP session on Claude's behalf:

  * forwards JSON-RPC both ways, attaching the current Mcp-Session-Id upstream;
  * on a 404 / unknown-session (or a dropped connection), silently re-runs the
    full handshake (initialize + notifications/initialized), then REPLAYS the
    request that failed — so the tool call just succeeds, a moment late;
  * if the editor isn't listening yet (connection refused), holds the request
    and retries with backoff, so you can start Claude before UE;
  * after a reconnect, emits notifications/tools/list_changed downstream so a
    recompiled/changed tool set is picked up with no intervention.

Pure standard library. No pip install, no MCP SDK — it forwards opaque JSON-RPC
envelopes, so it keeps working when the upstream tool set changes.

Config (env vars, all optional):
  UNREAL_MCP_URL          upstream endpoint (default http://127.0.0.1:8000/mcp)
  UNREAL_MCP_TOOL_TIMEOUT socket timeout for tool calls, seconds (default 600)
  UNREAL_MCP_QUICK_TIMEOUT socket timeout for handshake/list, seconds (default 30)
"""

import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

UPSTREAM_URL = os.environ.get("UNREAL_MCP_URL", "http://127.0.0.1:8000/mcp")
TOOL_TIMEOUT = float(os.environ.get("UNREAL_MCP_TOOL_TIMEOUT", "600"))
QUICK_TIMEOUT = float(os.environ.get("UNREAL_MCP_QUICK_TIMEOUT", "30"))

# Backoff schedule (seconds) used while the editor is unreachable. The last
# value repeats forever, so the bridge keeps trying until UE comes up.
BACKOFF = [0.5, 1, 2, 4, 8, 15]

_stdout_lock = threading.Lock()


def log(msg: str) -> None:
    """Diagnostics go to stderr — Claude Code captures it as MCP server log."""
    sys.stderr.write(f"[mcp-bridge] {msg}\n")
    sys.stderr.flush()


def write_downstream(message: dict) -> None:
    """Emit one newline-delimited JSON-RPC message to Claude Code."""
    line = json.dumps(message, separators=(",", ":"))
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class State:
    """Shared upstream-session state, guarded by a single lock.

    `epoch` increments every time we establish a new upstream session. A worker
    that fails on session N asks ensure_session to advance past epoch N; whoever
    wins the lock reinitializes once and everyone else reuses the result.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.session_id: str | None = None
        self.epoch = 0
        self.init_request: dict | None = None  # cached downstream `initialize`
        self.protocol_version: str | None = None


state = State()


# --------------------------------------------------------------------------- #
# Upstream HTTP
# --------------------------------------------------------------------------- #

def _http_post(payload: dict, session_id: str | None, timeout: float):
    """POST one JSON-RPC message upstream. Returns the response object.

    Raises urllib.error.HTTPError for 4xx/5xx (the error is itself a readable
    response) and urllib.error.URLError when the socket can't be established.
    """
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(UPSTREAM_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if session_id:
        req.add_header("Mcp-Session-Id", session_id)
    if state.protocol_version:
        req.add_header("Mcp-Protocol-Version", state.protocol_version)
    return urllib.request.urlopen(req, timeout=timeout)


def _post_retrying_refusals(payload: dict, session_id: str | None, timeout: float):
    """POST upstream, retrying only on connection failures (editor not up yet).

    HTTP errors (e.g. 404) are returned to the caller to interpret, not retried
    here — those mean the server answered and it's a session-level decision.
    """
    attempt = 0
    while True:
        try:
            return _http_post(payload, session_id, timeout)
        except urllib.error.HTTPError as err:
            return err  # 4xx/5xx: caller inspects .code and body
        except urllib.error.URLError as err:
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            if attempt == 0 or attempt % 5 == 0:
                log(f"upstream unreachable ({err.reason}); retrying in {delay}s "
                    f"— is the editor running at {UPSTREAM_URL}?")
            time.sleep(delay)
            attempt += 1


def _read_json_body(resp) -> dict | None:
    body = resp.read()
    if not body.strip():
        return None
    return json.loads(body)


def _iter_sse(resp):
    """Yield each JSON-RPC message carried on a text/event-stream response."""
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                yield json.loads("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith(":"):
            continue  # SSE comment / keep-alive
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # event:/id: fields are irrelevant to JSON-RPC framing
    if data_lines:
        yield json.loads("\n".join(data_lines))


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #

def _handshake(init_request: dict) -> tuple[str, dict | None]:
    """Run initialize + notifications/initialized upstream.

    Returns (session_id, initialize_result_message). Retries through connection
    refusals so the editor can still be launching.
    """
    resp = _post_retrying_refusals(init_request, session_id=None, timeout=QUICK_TIMEOUT)
    if isinstance(resp, urllib.error.HTTPError):
        raise RuntimeError(f"upstream initialize failed: HTTP {resp.code} {resp.read()!r}")

    session_id = resp.headers.get("Mcp-Session-Id")
    result = _read_json_body(resp)
    if not session_id:
        raise RuntimeError("upstream initialize returned no Mcp-Session-Id")
    if result:
        pv = result.get("result", {}).get("protocolVersion")
        if pv:
            state.protocol_version = pv

    # Drive the session to Initialized status so post-init methods are accepted.
    ack = _post_retrying_refusals(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id=session_id,
        timeout=QUICK_TIMEOUT,
    )
    if not isinstance(ack, urllib.error.HTTPError):
        try:
            ack.read()  # drain/close; notification returns an empty 202
        except Exception:
            pass
    return session_id, result


def ensure_session(min_epoch: int) -> tuple[str, int]:
    """Return a live (session_id, epoch), reinitializing if the epoch is stale.

    A worker passes the epoch it just failed on; if nobody has advanced past it
    yet, this call performs the (single) reinitialize under the lock.
    """
    with state.lock:
        if state.session_id is not None and state.epoch > min_epoch:
            return state.session_id, state.epoch
        if state.init_request is None:
            raise RuntimeError("cannot (re)initialize before downstream initialize")
        state.session_id, _ = _handshake(state.init_request)
        state.epoch += 1
        log(f"established upstream session (epoch {state.epoch})")
        return state.session_id, state.epoch


def handle_initialize(msg: dict) -> None:
    """Downstream `initialize`: cache it, handshake upstream once, echo result.

    Processed inline (not on a worker) so the session exists before any
    follow-up request is dispatched. Does NOT emit list_changed — this is the
    first connection, not a recovery. The upstream initialize result echoes the
    same request id we forwarded, so it maps straight back to Claude's call.
    """
    with state.lock:
        state.init_request = msg
        session_id, result = _handshake(msg)
        state.session_id = session_id
        state.epoch += 1
        epoch = state.epoch

    if result is not None:
        write_downstream(result)
    else:
        write_downstream({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {"code": -32603, "message": "upstream initialize returned no result"},
        })
    log(f"downstream initialized (epoch {epoch})")


def forward(msg: dict) -> None:
    """Forward a post-init request/notification upstream, recovering sessions."""
    is_request = "id" in msg
    method = msg.get("method", "")
    timeout = TOOL_TIMEOUT if method == "tools/call" else QUICK_TIMEOUT
    failed_epoch = -1
    recovered = False

    while True:
        session_id, epoch = ensure_session(failed_epoch)
        try:
            resp = _http_post(msg, session_id, timeout)
        except urllib.error.HTTPError as err:
            if err.code in (400, 404):
                # Stale or missing session — the editor was restarted. Force a
                # reinitialize past this epoch and replay.
                log(f"upstream session invalid (HTTP {err.code}); reinitializing")
                failed_epoch = epoch
                recovered = True
                continue
            body = _read_json_body(err)
            if is_request and body is not None:
                write_downstream(body)
            return
        except urllib.error.URLError as err:
            # Connection dropped mid-flight; treat like a dead session so the
            # next pass reinitializes (and rides out refusals via backoff).
            log(f"upstream connection lost ({err.reason}); reinitializing")
            failed_epoch = epoch
            recovered = True
            continue

        # Success — relay the response (SSE stream or single JSON body).
        if resp.headers.get_content_type() == "text/event-stream":
            for message in _iter_sse(resp):
                write_downstream(message)
        else:
            body = _read_json_body(resp)
            if is_request and body is not None:
                write_downstream(body)

        if recovered:
            # Tool set may have changed across the restart; nudge Claude to
            # refetch. Cheap and idempotent if nothing changed.
            write_downstream({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return


def worker(msg: dict) -> None:
    try:
        forward(msg)
    except Exception:  # noqa: BLE001 - last-resort guard, keep the bridge alive
        log("worker error:\n" + traceback.format_exc())
        if "id" in msg:
            write_downstream({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32603, "message": "bridge internal error"},
            })


def main() -> None:
    log(f"bridge up; upstream = {UPSTREAM_URL}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"dropping non-JSON line: {line[:120]!r}")
            continue

        method = msg.get("method")
        if method == "initialize":
            handle_initialize(msg)
        elif method == "notifications/initialized":
            # Already sent upstream as part of our handshake; swallow the
            # downstream copy so we don't double-drive the session.
            pass
        else:
            threading.Thread(target=worker, args=(msg,), daemon=True).start()

    log("stdin closed; bridge exiting")


if __name__ == "__main__":
    main()
