#!/usr/bin/env python3
"""End-to-end smoke tests for mcp_bridge.py — no Unreal Editor required.

The bridge exists to stay useful while the editor is down, so every test here
points it at a closed port and asserts it behaves. Stdlib only, no pytest: run
it the same way locally and in CI with `python tests/smoke_test.py`.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(ROOT, "mcp_bridge.py")
DEAD_URL = "http://127.0.0.1:59999/mcp"  # nothing listens here

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    suffix = f" — {detail}" if detail and not cond else ""
    print(f"  {status}  {name}{suffix}")
    if not cond:
        failures.append(name)


def spawn(cache_path, grace="3"):
    env = dict(
        os.environ,
        UNREAL_MCP_URL=DEAD_URL,
        UNREAL_MCP_CACHE=cache_path,
        UNREAL_MCP_GRACE=grace,
    )
    return subprocess.Popen(
        [sys.executable, BRIDGE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, bufsize=1,
    )


def reader(proc, sink):
    threading.Thread(target=lambda: [sink.append(line.strip()) for line in proc.stdout],
                     daemon=True).start()


def await_line(sink, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout and not sink:
        time.sleep(0.1)
    return sink[0] if sink else None


def test_survives_absent_editor():
    """Cold start with no cache and no editor must not crash the bridge."""
    print("\n[1] cold start, no cache, editor down")
    tmp = os.path.join(tempfile.mkdtemp(), "absent.json")
    proc = spawn(tmp)
    try:
        time.sleep(6)
        check("bridge still running after 6s", proc.poll() is None,
              f"exited with {proc.returncode}")
    finally:
        proc.kill()


def test_exits_when_stdin_closes():
    """A stdio server must exit when its client goes away, not linger."""
    print("\n[2] stdin closes -> clean exit")
    tmp = os.path.join(tempfile.mkdtemp(), "absent.json")
    proc = spawn(tmp)
    proc.stdin.close()
    try:
        proc.wait(timeout=15)
        check("exited promptly", True)
        check("exit status is 0", proc.returncode == 0, f"got {proc.returncode}")
    except subprocess.TimeoutExpired:
        check("exited promptly", False, "still running 15s after stdin closed")
        proc.kill()


def test_serves_initialize_from_cache():
    """The headline feature: answer a cold client while the editor is down."""
    print("\n[3] seeded cache, editor down -> initialize is answered")
    tmp = os.path.join(tempfile.mkdtemp(), "seeded.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "protocolVersion": "2025-11-25",
            "initializeResult": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "smoke", "title": "", "version": "0"},
            },
            "tools": [{"name": "smoke_tool", "description": "x",
                       "inputSchema": {"type": "object"}}],
            "savedAt": int(time.time()),
        }, f)

    proc = spawn(tmp)
    out = []
    reader(proc, out)
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "0"}},
        }) + "\n")
        proc.stdin.flush()

        line = await_line(out, timeout=20)
        check("got a response with the editor down", line is not None)
        if not line:
            return
        msg = json.loads(line)
        check("response is valid JSON-RPC 2.0", msg.get("jsonrpc") == "2.0", repr(msg)[:200])
        check("response matches request id", msg.get("id") == 1, repr(msg)[:200])
        check("response is a result, not an error", "result" in msg, repr(msg)[:200])
        check("result advertises a protocolVersion",
              bool(msg.get("result", {}).get("protocolVersion")), repr(msg)[:200])
    finally:
        proc.kill()


def test_handshake_retry_false_is_a_single_attempt():
    """Regression for #3: the editor vanishing mid-handshake must not hang.

    `_handshake(retry=False)` promises one attempt. The ack used to call the
    retrying form unconditionally with no deadline, so if the editor answered
    `initialize` and then went away, the ack looped forever — while two of the
    three callers held state.lock, deadlocking the bridge.
    """
    print("\n[4] editor answers initialize then vanishes -> handshake raises")

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)

    # Module constants are read at import time, so aim it at the fake first.
    os.environ["UNREAL_MCP_URL"] = f"http://127.0.0.1:{port}/mcp"
    sys.path.insert(0, ROOT)
    import mcp_bridge

    def serve_initialize_then_vanish():
        try:
            conn, _ = srv.accept()
            conn.recv(65536)
            body = json.dumps({"jsonrpc": "2.0", "id": 1,
                               "result": {"protocolVersion": "2025-11-25"}}).encode()
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Mcp-Session-Id: sess-1\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii")
            conn.sendall(head + body)
            conn.close()
        finally:
            srv.close()  # the ack now has nowhere to go

    threading.Thread(target=serve_initialize_then_vanish, daemon=True).start()

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "0"}}}

    outcome = []

    def run():
        try:
            mcp_bridge._handshake(init, retry=False)
            outcome.append("returned without completing the ack")
        except Exception as err:  # noqa: BLE001 - any raise beats blocking
            outcome.append(f"raised {type(err).__name__}")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=20)

    got = outcome[0] if outcome else "BLOCKED (still running after 20s)"
    check("retry=False raises instead of blocking", got.startswith("raised"), got)


if __name__ == "__main__":
    print(f"smoke: {BRIDGE}\nsmoke: upstream {DEAD_URL} (intentionally closed)")
    test_survives_absent_editor()
    test_exits_when_stdin_closes()
    test_serves_initialize_from_cache()
    test_handshake_retry_false_is_a_single_attempt()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("all smoke tests passed")
