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


def write_cache(path):
    """Write a minimal but valid cold-start cache to `path`."""
    with open(path, "w", encoding="utf-8") as f:
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
    write_cache(tmp)

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
    print("\n[5] editor answers initialize then vanishes -> handshake raises")

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


def test_survives_non_object_json_lines():
    """Regression for #4: one odd stdin line must not kill the bridge.

    Valid JSON that isn't an object — a batch array, a number, a string, null —
    used to reach msg.get() and raise AttributeError out of the main loop, so
    the process exited. Surviving isn't enough on its own, so this also proves
    the bridge still answers a real request afterwards.
    """
    print("\n[4] non-object JSON lines are dropped, not fatal")
    tmp = os.path.join(tempfile.mkdtemp(), "seeded.json")
    write_cache(tmp)

    proc = spawn(tmp)
    out = []
    reader(proc, out)
    try:
        for junk in ('[{"jsonrpc":"2.0","id":9,"method":"tools/list"}]',  # legal batch
                     "5", '"hello"', "null", "[]"):
            proc.stdin.write(junk + "\n")
            proc.stdin.flush()
        time.sleep(2)
        check("bridge still running after 5 non-object lines", proc.poll() is None,
              f"exited with {proc.returncode}")
        if proc.poll() is not None:
            return

        # Still functional, not merely alive.
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "0"}},
        }) + "\n")
        proc.stdin.flush()

        line = await_line(out, timeout=20)
        check("still answers initialize afterwards", line is not None)
        if line:
            msg = json.loads(line)
            check("and answers the right request", msg.get("id") == 1, repr(msg)[:200])
    finally:
        proc.kill()


def test_tool_call_hints():
    """New: call_tool errors get annotated from names the bridge has observed.

    Pure unit test against the observation helpers directly — no editor and no
    subprocess needed, since these functions only ever look at JSON-RPC
    messages already parsed into dicts. Module-level caches are reset first so
    this doesn't depend on (or pollute) test order.
    """
    print("\n[6] call_tool errors are annotated with previously-seen names")
    sys.path.insert(0, ROOT)
    import mcp_bridge

    mcp_bridge._known_toolsets.clear()
    mcp_bridge._toolset_tools.clear()
    real_load_cache = mcp_bridge.load_cache
    mcp_bridge.load_cache = lambda: {"tools": [{"name": "execute_python_code"},
                                               {"name": "call_tool"}]}

    # Nothing observed yet -> no hint, original error untouched.
    untouched = {"jsonrpc": "2.0", "id": 1,
                 "result": {"isError": True, "content": [{"type": "text",
                            "text": "Unknown tool get_selected_actors"}]}}
    out = mcp_bridge._observe_tool_call_reply(
        "call_tool", {"toolset_name": "Bogus.Toolset", "tool_name": "x"}, untouched)
    check("no hint before anything has been observed",
          out["result"]["content"][0]["text"] == "Unknown tool get_selected_actors")

    # Observe a list_toolsets reply, in the exact "- name: desc" shape the
    # editor sends — including a multi-line description with its OWN "- "
    # sub-bullets (regression: this used to be mistaken for two more
    # toolsets, e.g. "Enum value lookups").
    lt_reply = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text":
                "- editor_toolset.toolsets.scene.SceneTools: scene stuff\n"
                "- EditorToolset.EditorAppToolset: editor stuff\n"
                "- NiagaraToolsets.NiagaraToolset_Info: general info.\n\n"
                "Provides:\n"
                "- Enum value lookups\n"
                "- General usage information\n"}]}}
    mcp_bridge._observe_tool_call_reply("list_toolsets", {}, lt_reply)
    check("list_toolsets names are learned",
          mcp_bridge._known_toolsets == {"editor_toolset.toolsets.scene.SceneTools",
                                          "EditorToolset.EditorAppToolset",
                                          "NiagaraToolsets.NiagaraToolset_Info"},
          sorted(mcp_bridge._known_toolsets))
    check("prose sub-bullets inside a description are not mistaken for toolsets",
          "Enum value lookups" not in mcp_bridge._known_toolsets and
          "General usage information" not in mcp_bridge._known_toolsets)

    # No toolset_name at all (the single most common real failure) also gets
    # a hint, once at least one toolset is known.
    no_toolset = {"jsonrpc": "2.0", "id": 7,
                  "result": {"isError": True, "content": [{"type": "text",
                             "text": "Unknown tool ListActorsInWorld"}]}}
    out = mcp_bridge._observe_tool_call_reply(
        "call_tool", {"tool_name": "ListActorsInWorld"}, no_toolset)
    hinted = out["result"]["content"][0]["text"]
    check("missing toolset_name gets a hint naming the known toolsets",
          "[bridge hint]" in hinted and "SceneTools" in hinted, hinted)

    # A real top-level tool called with no toolset_name is a correct call that
    # failed on its own terms — no hint that would send the model guessing.
    top_level_fail = {"jsonrpc": "2.0", "id": 8,
                      "result": {"isError": True, "content": [{"type": "text",
                                 "text": "Traceback: NameError: foo"}]}}
    out = mcp_bridge._observe_tool_call_reply(
        "call_tool", {"tool_name": "execute_python_code"}, top_level_fail)
    check("a failing top-level tool is not told it does not exist",
          out["result"]["content"][0]["text"] == "Traceback: NameError: foo",
          out["result"]["content"][0]["text"])

    # An unknown toolset now gets a hint listing the known ones.
    bad_toolset = {"jsonrpc": "2.0", "id": 3,
                   "result": {"isError": True, "content": [{"type": "text",
                              "text": "Unknown tool get_selected_actors"}]}}
    out = mcp_bridge._observe_tool_call_reply(
        "call_tool", {"toolset_name": "EditorToolset.EditorAppToolset.Wrong", "tool_name": "x"},
        bad_toolset)
    hinted = out["result"]["content"][0]["text"]
    check("unknown toolset gets a hint naming the known ones",
          "[bridge hint]" in hinted and "SceneTools" in hinted, hinted)

    # Observe a describe_toolset reply for one real toolset. The editor
    # reports tool names qualified, but call_tool only takes them bare.
    dt_reply = {"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text":
                '{"name":"editor_toolset.toolsets.scene.SceneTools","tools":['
                '{"name":"editor_toolset.toolsets.scene.SceneTools.save_actor"},'
                '{"name":"editor_toolset.toolsets.scene.SceneTools.get_actors"},'
                '{"name":"editor_toolset.toolsets.scene.SceneTools.is_checked_out"}]}'}]}}
    mcp_bridge._observe_tool_call_reply("describe_toolset", {}, dt_reply)
    check("describe_toolset tool names are learned bare",
          mcp_bridge._toolset_tools.get("editor_toolset.toolsets.scene.SceneTools") ==
          {"save_actor", "get_actors", "is_checked_out"},
          sorted(mcp_bridge._toolset_tools.get("editor_toolset.toolsets.scene.SceneTools") or []))

    # A bad tool name inside a KNOWN toolset now gets the real tool names.
    bad_tool = {"jsonrpc": "2.0", "id": 5,
                "result": {"isError": True, "content": [{"type": "text",
                           "text": "Unknown tool get_selected_actors"}]}}
    out = mcp_bridge._observe_tool_call_reply(
        "call_tool",
        {"toolset_name": "editor_toolset.toolsets.scene.SceneTools",
         "tool_name": "get_selected_actors"},
        bad_tool)
    hinted = out["result"]["content"][0]["text"]
    check("known toolset, bad tool gets a hint naming the real tool names",
          "[bridge hint]" in hinted and "get_actors" in hinted, hinted)
    check("and lists them in the bare form call_tool accepts",
          "SceneTools.get_actors" not in hinted, hinted)

    # The qualified name describe_toolset reported, copied verbatim, is
    # rejected by the editor: point straight at the bare name, not the list.
    qualified = {"jsonrpc": "2.0", "id": 10,
                 "result": {"isError": True, "content": [{"type": "text",
                            "text": "Unknown tool editor_toolset.toolsets.scene."
                                    "SceneTools.get_actors"}]}}
    out = mcp_bridge._observe_tool_call_reply(
        "call_tool",
        {"toolset_name": "editor_toolset.toolsets.scene.SceneTools",
         "tool_name": "editor_toolset.toolsets.scene.SceneTools.get_actors"},
        qualified)
    hinted = out["result"]["content"][0]["text"]
    check("qualified name of a known tool gets a bare-name hint",
          "[bridge hint]" in hinted and "tool_name 'get_actors'" in hinted
          and "save_actor" not in hinted, hinted)
    # Spelling out the toolset too: told only "use the bare name", a local
    # model dropped toolset_name along with the qualifier.
    check("and it keeps the toolset in the call it asks for",
          "pass toolset_name 'editor_toolset.toolsets.scene.SceneTools' and "
          "tool_name 'get_actors'" in hinted, hinted)

    # The right tool, called by its bare name as call_tool takes it, failing
    # at runtime gets no hint.
    right_tool_fail = {"jsonrpc": "2.0", "id": 9,
                       "result": {"isError": True, "content": [{"type": "text",
                                  "text": "Actor not found"}]}}
    out = mcp_bridge._observe_tool_call_reply(
        "call_tool",
        {"toolset_name": "editor_toolset.toolsets.scene.SceneTools",
         "tool_name": "get_actors"},
        right_tool_fail)
    check("a known tool failing at runtime is left untouched",
          out["result"]["content"][0]["text"] == "Actor not found",
          out["result"]["content"][0]["text"])

    def hint_for(args):
        """The text a failed call_tool with these args comes back with."""
        reply = {"jsonrpc": "2.0", "id": 0,
                 "result": {"isError": True, "content": [{"type": "text",
                            "text": "Unknown tool"}]}}
        out = mcp_bridge._observe_tool_call_reply("call_tool", args, reply)
        return out["result"]["content"][0]["text"]

    scene = "editor_toolset.toolsets.scene.SceneTools"

    # No toolset_name, but the qualified form holds both halves: split it.
    hinted = hint_for({"tool_name": scene + ".get_actors"})
    check("qualified name with no toolset_name is split into its two halves",
          f"pass toolset_name '{scene}' and tool_name 'get_actors'" in hinted, hinted)

    # No toolset_name, bare name of a described tool: name its toolset, not
    # every known one.
    hinted = hint_for({"tool_name": "get_actors"})
    check("known tool with no toolset_name is pointed at its toolset",
          f"'{scene}' has it" in hinted and "EditorAppToolset" not in hinted, hinted)

    # A toolset name the session never saw, for a tool it has: same.
    hinted = hint_for({"toolset_name": "SceneTools", "tool_name": "get_actors"})
    check("unknown toolset, known tool is pointed at the right toolset",
          f"'{scene}' has it" in hinted and "EditorAppToolset" not in hinted, hinted)

    # Two more toolsets, named as the editor has them: EditorAppToolset is
    # CamelCase, and is_checked_out is in AssetTools as well as SceneTools.
    for ts, tools in [("EditorToolset.EditorAppToolset",
                       ["GetSelectedActors", "GetCameraTransform", "SetCameraTransform"]),
                      ("editor_toolset.toolsets.asset.AssetTools",
                       ["is_checked_out", "save_assets"])]:
        text = json.dumps({"name": ts, "tools": [{"name": f"{ts}.{t}"} for t in tools]})
        mcp_bridge._observe_tool_call_reply(
            "describe_toolset", {},
            {"jsonrpc": "2.0", "id": 0, "result": {"content": [{"type": "text", "text": text}]}})

    # A real tool asked of the wrong toolset, in the other naming style.
    hinted = hint_for({"toolset_name": scene, "tool_name": "get_selected_actors"})
    check("tool from another toolset is pointed at, whatever its naming style",
          "pass toolset_name 'EditorToolset.EditorAppToolset' and tool_name "
          "'GetSelectedActors'" in hinted, hinted)

    # A tool several toolsets have: list each, let the model pick.
    hinted = hint_for({"tool_name": "is_checked_out"})
    check("tool in several toolsets lists each of them",
          "several have it" in hinted and "AssetTools" in hinted
          and "SceneTools" in hinted, hinted)

    # Seen from a local model in Cline: the qualified form AND the wrong
    # toolset ("...ActorTools.find_actors" asked of ActorTools). The lookup
    # goes by the bare part, so it still finds the toolset that has it.
    asset = "editor_toolset.toolsets.asset.AssetTools"
    hinted = hint_for({"toolset_name": asset, "tool_name": asset + ".get_actors"})
    check("qualified name in the wrong toolset is pointed at the right one",
          f"pass toolset_name '{scene}' and tool_name 'get_actors'" in hinted, hinted)

    # A short qualifier, as docs write it ("AgentSkillToolset.ListSkills"),
    # with no toolset_name: found by its bare part all the same.
    hinted = hint_for({"tool_name": "EditorAppToolset.GetSelectedActors"})
    check("short-qualified name with no toolset_name finds its toolset",
          "pass toolset_name 'EditorToolset.EditorAppToolset' and tool_name "
          "'GetSelectedActors'" in hinted, hinted)

    # A near miss of a tool in the same toolset is suggested, before the list.
    hinted = hint_for({"toolset_name": scene, "tool_name": "get_actor"})
    check("near-miss tool name gets a did-you-mean",
          "Did you mean get_actors?" in hinted, hinted)
    app = "EditorToolset.EditorAppToolset"
    hinted = hint_for({"toolset_name": app, "tool_name": "get_camera_transform"})
    check("so does the same tool in the other naming style",
          "Did you mean GetCameraTransform?" in hinted, hinted)

    # Never a different verb, however close the spelling: GetCameraTransform
    # is within the cutoff of this typo too, and so is save_actor of
    # "set_actor". Taking either would run another action.
    hinted = hint_for({"toolset_name": app, "tool_name": "SetCameraTransfrom"})
    check("only a name with the same verb is suggested",
          "Did you mean SetCameraTransform?" in hinted, hinted)
    hinted = hint_for({"toolset_name": scene, "tool_name": "set_actor"})
    check("a different verb is never suggested",
          "Did you mean" not in hinted, hinted)

    # A successful call_tool is never touched.
    ok_reply = {"jsonrpc": "2.0", "id": 6,
                "result": {"content": [{"type": "text", "text": "all good"}]}}
    out = mcp_bridge._observe_tool_call_reply(
        "call_tool", {"toolset_name": "x", "tool_name": "y"}, dict(ok_reply))
    check("a successful call_tool reply is left untouched",
          out["result"]["content"][0]["text"] == "all good")

    mcp_bridge.load_cache = real_load_cache
    mcp_bridge._known_toolsets.clear()
    mcp_bridge._toolset_tools.clear()


def test_resource_templates_answered_locally():
    """resources/templates/list is answered by the bridge, editor or not.

    The editor advertises a resources capability but answers this method with
    an error, which some clients (e.g. Cline) take as a reason to redo the
    whole handshake in a loop. The bridge answers it with an empty list.
    """
    print("\n[7] resources/templates/list -> empty list, editor down")
    tmp = os.path.join(tempfile.mkdtemp(), "templates.json")
    write_cache(tmp)

    proc = spawn(tmp)
    out = []
    reader(proc, out)
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 42, "method": "resources/templates/list",
            "params": {},
        }) + "\n")
        proc.stdin.flush()

        line = await_line(out, timeout=10)
        check("got a response with the editor down", line is not None)
        if not line:
            return
        msg = json.loads(line)
        check("response matches request id", msg.get("id") == 42, repr(msg)[:200])
        check("result is an empty template list",
              msg.get("result") == {"resourceTemplates": []}, repr(msg)[:200])
    finally:
        proc.kill()


def test_compact_describe():
    """UNREAL_MCP_COMPACT_DESCRIBE trims descriptions, never what builds args.

    Unit test on the reply rewrite itself, with a describe_toolset reply
    shaped like the editor's: docstring descriptions with Args:/Returns:
    sections, and the UObject reference schema every object argument repeats.
    """
    print("\n[9] compact describe_toolset trims descriptions only")
    sys.path.insert(0, ROOT)
    import mcp_bridge

    check("off unless UNREAL_MCP_COMPACT_DESCRIBE=1", mcp_bridge.COMPACT_DESCRIBE is False)

    ref = {"type": "object", "title": "/Script/Engine.Actor",
           "description": "Represents a reference to a UObject or UClass.",
           "properties": {"refPath": {
               "type": "string", "description": "The reference stored as a soft path string."}},
           "required": ["refPath"]}
    data = {"name": "Smoke.SceneTools", "version": "1.0", "description": "Scene tools.", "tools": [
        {"description": "Saves the actor to disk.\n\n        Args:\n            actor: The actor "
                        "to save.\n\n        Returns:\n            True if it was saved.",
         "name": "Smoke.SceneTools.save_actor",
         "inputSchema": {"type": "object", "properties": {"actor": ref}, "required": ["actor"]},
         "outputSchema": {"type": "object", "properties": {"returnValue": {"type": "boolean"}}}},
        {"description": "Moves an actor.\n\n        Args:\n            actor: The actor to move."
                        "\n\n        Returns:\n            Where it ended up.",
         "name": "Smoke.SceneTools.move_actor",
         "inputSchema": {"type": "object", "properties": {
             "actor": ref,
             "mode": {"type": "string", "title": "EMoveMode", "enum": ["Snap", "Free"],
                      "default": "Snap", "description": "How to move it."},
             "description": {"type": "string"}},
             "required": ["actor", "mode"]}},
        # As BlueprintTools' add_object_function_param has it: no blank line
        # between the Args: lines and Returns:.
        {"description": "Adds a param.\n\n        Args:\n            name: Its name.\n"
                        "        Returns:\n            The new pin.",
         "name": "Smoke.SceneTools.add_param",
         "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
         "outputSchema": {"type": "object", "properties": {"returnValue": {"type": "string"}}}},
    ]}
    reply = {"jsonrpc": "2.0", "id": 1,
             "result": {"content": [{"type": "text", "text": json.dumps(data)}]}}
    out = mcp_bridge._compact_describe_reply(json.loads(json.dumps(reply)))
    got = json.loads(out["result"]["content"][0]["text"])
    save, move, add = got["tools"]
    moved_actor = move["inputSchema"]["properties"]["actor"]

    check("a Returns: section is dropped when an outputSchema says the same",
          "Returns:" not in save["description"]
          and "True if it was saved" not in save["description"], save["description"])
    check("the summary and Args: section are kept as they were",
          save["description"] == data["tools"][0]["description"].split("\n\n        Returns:")[0],
          save["description"])
    check("so is one right after the Args: lines",
          add["description"] == "Adds a param.\n\n        Args:\n            name: Its name.",
          add["description"])
    check("a tool with no outputSchema keeps its Returns: section",
          move["description"] == data["tools"][1]["description"], move["description"])
    check("a schema description already seen in the reply is dropped",
          "description" not in moved_actor
          and "description" not in moved_actor["properties"]["refPath"], json.dumps(moved_actor))
    check("its first occurrence is kept",
          save["inputSchema"]["properties"]["actor"]["description"] == ref["description"],
          json.dumps(save["inputSchema"]["properties"]["actor"]))
    check("a description seen once is kept",
          move["inputSchema"]["properties"]["mode"].get("description") == "How to move it.")

    def without_descriptions(node):
        """Everything but description strings: what a model builds arguments from."""
        if isinstance(node, dict):
            return {k: without_descriptions(v) for k, v in node.items()
                    if not (k == "description" and isinstance(v, str))}
        if isinstance(node, list):
            return [without_descriptions(v) for v in node]
        return node

    check("names, types, properties, required, enums, defaults and titles are untouched "
          "(a property named description included)",
          without_descriptions(got) == without_descriptions(data))

    error_reply = {"jsonrpc": "2.0", "id": 2, "result": {"isError": True, "content": [
        {"type": "text", "text": "Toolset 'Smoke.Nope' not found"}]}}
    text_reply = {"jsonrpc": "2.0", "id": 3, "result": {"content": [
        {"type": "text", "text": "not JSON at all"}]}}
    for name, msg in (("an error reply", error_reply), ("a non-JSON reply", text_reply)):
        before = json.dumps(msg)
        check(f"{name} is forwarded as it was",
              json.dumps(mcp_bridge._compact_describe_reply(msg)) == before)


if __name__ == "__main__":
    print(f"smoke: {BRIDGE}\nsmoke: upstream {DEAD_URL} (intentionally closed)")
    test_survives_absent_editor()
    test_exits_when_stdin_closes()
    test_serves_initialize_from_cache()
    test_survives_non_object_json_lines()
    test_handshake_retry_false_is_a_single_attempt()
    test_tool_call_hints()
    test_compact_describe()
    test_resource_templates_answered_locally()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("all smoke tests passed")
