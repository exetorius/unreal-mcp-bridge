#!/usr/bin/env python3
"""
unreal-mcp-bridge — a stdio<->HTTP relay that keeps Claude Code connected to
Unreal Engine's built-in Model Context Protocol server across editor restarts.

The problem
-----------
UE's MCP server (Engine/Plugins/Experimental/ModelContextProtocol) is a
Streamable-HTTP server. Every post-initialize request must carry an
`Mcp-Session-Id` naming a session that lives in the editor's memory. When you
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

Transport note
--------------
UE returns `tools/call` results as a `text/event-stream` with NO Content-Length
and a keep-alive connection. Python's http.client/urllib cannot read a body
framed that way (it reports an empty body), so this bridge speaks HTTP over a
raw socket and reads the SSE stream itself, stopping as soon as the response for
the in-flight request arrives. Non-streaming replies (initialize, tools/list)
carry a Content-Length and are read normally.

Pure standard library. No pip install, no MCP SDK — it forwards opaque JSON-RPC
envelopes, so it keeps working when the upstream tool set changes.

Cold-start tool cache
---------------------
Claude Code registers an MCP server's tools once, from the initialize/tools-list
handshake at session start. If the editor is down at that moment, the honest
answer is "no tools", and the harness freezes that empty set for the whole
session — a later list_changed is ignored for a server that handshaked empty.

To survive that, the bridge persists the last live `initialize` result and
`tools/list` to a small JSON file (default: tool_cache.json next to this
script). When the editor is unreachable at startup it answers initialize and
tools/list *from that cache immediately*, so Claude registers the real,
non-empty tool set. It then connects upstream in the background and, if the live
tool set differs from the cache, emits notifications/tools/list_changed — which
Claude now honors because the initial registration was non-empty. The only case
still needing the editor is the very first run ever, when no cache exists yet.

Config (env vars, all optional):
  UNREAL_MCP_URL           upstream endpoint (default http://127.0.0.1:8000/mcp)
  UNREAL_MCP_TOOL_TIMEOUT  socket timeout for tool calls, seconds (default 600)
  UNREAL_MCP_QUICK_TIMEOUT socket timeout for handshake/list, seconds (default 30)
  UNREAL_MCP_CACHE         tool-cache path (default tool_cache.json beside script)
"""

import json
import os
import socket
import sys
import threading
import time
import traceback
from urllib.parse import urlparse

UPSTREAM_URL = os.environ.get("UNREAL_MCP_URL", "http://127.0.0.1:8000/mcp")
TOOL_TIMEOUT = float(os.environ.get("UNREAL_MCP_TOOL_TIMEOUT", "600"))
QUICK_TIMEOUT = float(os.environ.get("UNREAL_MCP_QUICK_TIMEOUT", "30"))
CONNECT_TIMEOUT = 5.0

_parsed = urlparse(UPSTREAM_URL)
HOST = _parsed.hostname or "127.0.0.1"
PORT = _parsed.port or 8000
PATH = _parsed.path or "/mcp"

CACHE_PATH = os.environ.get(
    "UNREAL_MCP_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_cache.json"),
)

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
        self.bg_started = False  # a background connect/reconcile is in flight


state = State()

_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Cold-start tool cache (see module docstring)
# --------------------------------------------------------------------------- #

def load_cache() -> dict | None:
    """Return the on-disk cache dict, or None if absent/unreadable."""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_cache(*, protocol_version: str | None = None,
               initialize_result: dict | None = None,
               tools_result: dict | None = None) -> None:
    """Merge the given fields into the cache and write it atomically."""
    with _cache_lock:
        data = load_cache() or {}
        if initialize_result is not None:
            data["initializeResult"] = initialize_result
        if tools_result is not None:
            data["tools"] = tools_result.get("tools", [])
        if protocol_version:
            data["protocolVersion"] = protocol_version
        data["savedAt"] = int(time.time())
        tmp = CACHE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, CACHE_PATH)
        except OSError as err:
            log(f"could not write tool cache: {err}")


def _tools_signature(tools: list) -> str:
    """A stable fingerprint of a tool set, sensitive to names and schemas."""
    return json.dumps(sorted(tools, key=lambda t: t.get("name", "")), sort_keys=True)


# --------------------------------------------------------------------------- #
# Raw-socket HTTP
# --------------------------------------------------------------------------- #

class _SockReader:
    """Buffered line/exact reader over a socket. readline/read return b'' at EOF."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    def _fill(self) -> bool:
        chunk = self._sock.recv(65536)
        if not chunk:
            return False
        self._buf += chunk
        return True

    def readline(self) -> bytes:
        while b"\n" not in self._buf:
            if not self._fill():
                line, self._buf = self._buf, b""
                return line
        idx = self._buf.index(b"\n") + 1
        line, self._buf = self._buf[:idx], self._buf[idx:]
        return line

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if not self._fill():
                break
        data, self._buf = self._buf[:n], self._buf[n:]
        return data


class Response:
    """A response with headers read and the body still on the wire."""

    def __init__(self, sock: socket.socket, status: int, headers: dict[str, str],
                 reader: _SockReader) -> None:
        self.sock = sock
        self.status = status
        self.headers = headers
        self._reader = reader

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def messages(self):
        """Yield JSON-RPC messages from the body, whatever the framing.

        text/event-stream: yield one message per SSE `data:` event, streaming as
        they arrive (the server may hold the connection open afterward — callers
        stop once they have the reply they want). Otherwise: read the single
        Content-Length body and yield it.
        """
        ctype = self.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            data_lines: list[str] = []
            while True:
                raw = self._reader.readline()
                if raw == b"":
                    break  # server closed the stream
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        yield json.loads("\n".join(data_lines))
                        data_lines = []
                elif line.startswith(":"):
                    continue  # SSE comment / keep-alive
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                # event:/id: SSE fields are irrelevant to JSON-RPC framing
            if data_lines:
                yield json.loads("\n".join(data_lines))
        else:
            length = int(self.headers.get("content-length", "0") or "0")
            body = self._reader.read_exact(length) if length else b""
            if body.strip():
                yield json.loads(body)


def _http_request(payload: dict, session_id: str | None, timeout: float) -> Response:
    """POST one JSON-RPC message over a fresh socket; return after reading headers.

    Raises OSError (incl. ConnectionRefusedError) if the socket can't be
    established, and ConnectionError if the server closes before sending a
    status line (which is how a mid-restart editor looks).
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    lines = [
        f"POST {PATH} HTTP/1.1",
        f"Host: {HOST}:{PORT}",
        "Content-Type: application/json",
        "Accept: application/json, text/event-stream",
        f"Content-Length: {len(body)}",
    ]
    if session_id:
        lines.append(f"Mcp-Session-Id: {session_id}")
    if state.protocol_version:
        lines.append(f"Mcp-Protocol-Version: {state.protocol_version}")
    lines.append("Connection: keep-alive")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body

    sock = socket.create_connection((HOST, PORT), timeout=CONNECT_TIMEOUT)
    sock.settimeout(timeout)
    sock.sendall(request)

    reader = _SockReader(sock)
    status_line = reader.readline().decode("iso-8859-1").rstrip("\r\n")
    if not status_line:
        sock.close()
        raise ConnectionError("upstream closed connection before responding")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    headers: dict[str, str] = {}
    while True:
        line = reader.readline().decode("iso-8859-1").rstrip("\r\n")
        if line == "":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return Response(sock, status, headers, reader)


def _http_request_retrying(payload: dict, session_id: str | None, timeout: float) -> Response:
    """As _http_request, but ride out connection refusals (editor still starting)."""
    attempt = 0
    while True:
        try:
            return _http_request(payload, session_id, timeout)
        except (ConnectionRefusedError, ConnectionResetError, ConnectionError,
                socket.gaierror) as err:
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            if attempt == 0 or attempt % 5 == 0:
                log(f"upstream unreachable ({err}); retrying in {delay}s "
                    f"— is the editor running at {UPSTREAM_URL}?")
            time.sleep(delay)
            attempt += 1


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #

def _handshake(init_request: dict, retry: bool = True) -> tuple[str, dict | None]:
    """Run initialize + notifications/initialized upstream.

    Returns (session_id, initialize_result_message). With retry=True (default)
    it rides out connection refusals so the editor can still be launching; with
    retry=False it makes a single attempt and lets OSError/ConnectionError
    propagate — used for the bounded cold-start probe that decides whether to
    fall back to the cache.
    """
    send = _http_request_retrying if retry else _http_request
    resp = send(init_request, None, QUICK_TIMEOUT)
    try:
        if resp.status >= 400:
            raise RuntimeError(f"upstream initialize failed: HTTP {resp.status}")
        session_id = resp.headers.get("mcp-session-id")
        result = next(resp.messages(), None)
    finally:
        resp.close()
    if not session_id:
        raise RuntimeError("upstream initialize returned no Mcp-Session-Id")
    if result:
        pv = result.get("result", {}).get("protocolVersion")
        if pv:
            state.protocol_version = pv

    # Drive the session to Initialized status so post-init methods are accepted.
    ack = _http_request_retrying(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id, QUICK_TIMEOUT,
    )
    ack.close()
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


def ensure_session_quick(min_epoch: int) -> tuple[str, int]:
    """Like ensure_session, but a single bounded attempt (no infinite retry).

    Raises OSError/ConnectionError if the editor is unreachable, so callers can
    fall back to the cache instead of blocking.
    """
    with state.lock:
        if state.session_id is not None and state.epoch > min_epoch:
            return state.session_id, state.epoch
        if state.init_request is None:
            raise RuntimeError("cannot (re)initialize before downstream initialize")
        state.session_id, _ = _handshake(state.init_request, retry=False)
        state.epoch += 1
        log(f"established upstream session (epoch {state.epoch})")
        return state.session_id, state.epoch


def _upstream_roundtrip(msg: dict, timeout: float) -> dict | None:
    """Send one request upstream and return its matching reply message.

    Bounded (no infinite reconnect): recovers once from a stale session, then
    lets connection failures propagate so the caller can serve the cache. Does
    not write anything downstream — used for reconciliation and cache-guarded
    tools/list.
    """
    target_id = msg.get("id")
    failed_epoch = -1
    for _ in range(2):
        session_id, epoch = ensure_session_quick(failed_epoch)
        resp = _http_request(msg, session_id, timeout)
        if resp.status in (400, 404):
            resp.close()
            failed_epoch = epoch
            continue
        try:
            if resp.status >= 400:
                return next(resp.messages(), None)
            for message in resp.messages():
                if message.get("id") == target_id:
                    return message
            return None
        finally:
            resp.close()
    return None


def handle_initialize(msg: dict) -> None:
    """Downstream `initialize`: answer live if the editor is up, else from cache.

    Processed inline (not on a worker) so the session exists before any
    follow-up request is dispatched. Tries a single bounded upstream handshake;
    if that fails and we have a cached initialize result, answers from cache
    immediately and connects in the background so Claude registers the real,
    non-empty tool set. Only the very first run ever (no cache) blocks for the
    editor. Does NOT emit list_changed here — the first connection is not a
    recovery.
    """
    with state.lock:
        state.init_request = msg

    # 1) Editor up? Answer live and refresh the cache.
    try:
        with state.lock:
            session_id, result = _handshake(msg, retry=False)
            state.session_id = session_id
            state.epoch += 1
            epoch = state.epoch
    except (OSError, ConnectionError, RuntimeError) as err:
        result = None
        epoch = None
        live_err = err
    else:
        _emit_initialize(msg, result)
        save_cache(protocol_version=state.protocol_version,
                   initialize_result=result.get("result") if result else None)
        log(f"downstream initialized live (epoch {epoch})")
        return

    # 2) Editor down but we have a cache — answer from it, connect in background.
    cache = load_cache()
    if cache and cache.get("initializeResult"):
        state.protocol_version = cache.get("protocolVersion")
        write_downstream({"jsonrpc": "2.0", "id": msg.get("id"),
                          "result": cache["initializeResult"]})
        log(f"upstream down at startup ({live_err}); answered initialize from "
            f"cache; connecting in background")
        _ensure_background_connect()
        return

    # 3) No cache — the first run ever must reach the editor. Block-retry.
    log(f"upstream down and no cache ({live_err}); waiting for the editor")
    with state.lock:
        session_id, result = _handshake(msg, retry=True)
        state.session_id = session_id
        state.epoch += 1
        epoch = state.epoch
    _emit_initialize(msg, result)
    save_cache(protocol_version=state.protocol_version,
               initialize_result=result.get("result") if result else None)
    log(f"downstream initialized (epoch {epoch})")


def _emit_initialize(msg: dict, result: dict | None) -> None:
    """Send the initialize reply downstream (or an error if upstream gave none)."""
    if result is not None:
        write_downstream(result)
    else:
        write_downstream({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {"code": -32603, "message": "upstream initialize returned no result"},
        })


def _ensure_background_connect() -> None:
    """Start the background connect/reconcile once, if not already running."""
    with state.lock:
        if state.bg_started or state.session_id is not None:
            return
        state.bg_started = True
    threading.Thread(target=_background_connect, daemon=True).start()


def _background_connect() -> None:
    """Establish the real upstream session, then reconcile the cached tool set.

    Runs after we answered initialize from cache. Retries WITHOUT holding
    state.lock — a lock-held retry would starve cache-guarded tools/list, which
    is exactly what has to stay responsive while the editor is still down. The
    lock is taken only briefly, to publish the session if nobody beat us to it.
    """
    attempt = 0
    while True:
        try:
            session_id, _ = _handshake(state.init_request, retry=False)
        except (OSError, ConnectionError, RuntimeError) as err:
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            if attempt == 0 or attempt % 5 == 0:
                log(f"background connect waiting for editor ({err}); "
                    f"retry in {delay}s")
            time.sleep(delay)
            attempt += 1
            continue
        break

    with state.lock:
        if state.session_id is None:
            state.session_id = session_id
            state.epoch += 1
        epoch = state.epoch
    log(f"background upstream session established (epoch {epoch}); reconciling tools")
    try:
        reconcile_tools()
    except Exception:  # noqa: BLE001 - keep the bridge alive
        log("tool reconcile failed:\n" + traceback.format_exc())


def reconcile_tools() -> None:
    """Fetch the live tool set and, if it differs from the cache, notify Claude."""
    req = {"jsonrpc": "2.0", "id": "__bridge_reconcile__",
           "method": "tools/list", "params": {}}
    try:
        reply = _upstream_roundtrip(req, QUICK_TIMEOUT)
    except (OSError, ConnectionError, RuntimeError) as err:
        log(f"tool reconcile skipped ({err})")
        return
    if not reply or "result" not in reply:
        return
    live = reply["result"].get("tools", [])
    cached = (load_cache() or {}).get("tools") or []
    save_cache(protocol_version=state.protocol_version,
               tools_result=reply["result"])
    if _tools_signature(live) != _tools_signature(cached):
        log(f"tool set changed since last run ({len(cached)} -> {len(live)}); "
            f"notifying Claude")
        write_downstream({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    else:
        log("tool set unchanged since last run")


def handle_tools_list(msg: dict) -> None:
    """Answer tools/list live if the editor is up, else from the cache.

    The cache path is what makes cold start work: right after an initialize we
    answered from cache, Claude asks for tools/list while the editor is still
    down, and this serves the cached set so the registration is non-empty.
    """
    try:
        reply = _upstream_roundtrip(msg, QUICK_TIMEOUT)
    except (OSError, ConnectionError, RuntimeError):
        reply = None
    if reply is not None and "result" in reply:
        write_downstream(reply)
        save_cache(protocol_version=state.protocol_version,
                   tools_result=reply["result"])
        return

    cache = load_cache()
    if cache and cache.get("tools") is not None:
        write_downstream({"jsonrpc": "2.0", "id": msg.get("id"),
                          "result": {"tools": cache["tools"]}})
        log("answered tools/list from cache (upstream unreachable)")
        _ensure_background_connect()
        return

    # No cache and upstream down — fall back to the blocking path (waits for UE).
    forward(msg)


def forward(msg: dict) -> None:
    """Forward a post-init request/notification upstream, recovering sessions."""
    is_request = "id" in msg
    target_id = msg.get("id")
    method = msg.get("method", "")
    timeout = TOOL_TIMEOUT if method == "tools/call" else QUICK_TIMEOUT
    failed_epoch = -1
    recovered = False

    while True:
        session_id, epoch = ensure_session(failed_epoch)
        try:
            resp = _http_request(msg, session_id, timeout)
        except (OSError, ConnectionError) as err:
            # Socket refused/reset/closed — editor was (re)started. Reinitialize
            # past this epoch (which rides out refusals) and replay.
            log(f"upstream connection failed ({err}); reinitializing")
            failed_epoch = epoch
            recovered = True
            continue

        if resp.status in (400, 404):
            # Stale or missing session — the editor was restarted.
            log(f"upstream session invalid (HTTP {resp.status}); reinitializing")
            resp.close()
            failed_epoch = epoch
            recovered = True
            continue

        try:
            if resp.status >= 400:
                body = next(resp.messages(), None)
                if is_request and body is not None:
                    write_downstream(body)
                return
            # Success — relay messages until we've delivered our reply. The SSE
            # stream may stay open for server-push after the reply; we stop once
            # the matching id arrives so the call doesn't hang on it.
            for message in resp.messages():
                write_downstream(message)
                if is_request and message.get("id") == target_id:
                    break
        finally:
            resp.close()

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


def _tools_list_worker(msg: dict) -> None:
    try:
        handle_tools_list(msg)
    except Exception:  # noqa: BLE001 - last-resort guard, keep the bridge alive
        log("tools/list worker error:\n" + traceback.format_exc())
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
        elif method == "tools/list":
            # Cache-guarded so a cold start with the editor down still registers
            # a non-empty tool set.
            threading.Thread(target=_tools_list_worker, args=(msg,), daemon=True).start()
        else:
            threading.Thread(target=worker, args=(msg,), daemon=True).start()

    log("stdin closed; bridge exiting")


if __name__ == "__main__":
    main()
