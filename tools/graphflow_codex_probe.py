"""Verify the effective Codex project MCP and optionally its trusted hook."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .graphflow_codex import _hook_command
else:
    from graphflow_codex import _hook_command


LEGACY = {"graphflow", "user-graphflow"}
REQUIRED = {"graphflow_context", "graphflow_read", "get_core", "recall_index"}
COMPAT = {"gf_read", "gf_edit", "gf_exec", "gf_macro", "graphflow_diagnose", "graphflow_artifact", "graphflow_graph",
          "graphflow_skill_insights", "graphflow_skill_guide", "graphflow_run", "graphflow_report_outcome",
          "graphflow_insight", "graphflow_impact", "graphflow_plan", "graphflow_workflow_preview"}
APPROVAL_ONLY = {"graphflow_edit_apply", "graphflow_edit_rollback", "graphflow_test_run"}
HOOK_HINT = "use the starwish MCP graphflow_context tool first"
_ENGINE_MARKER = re.compile(r"(?<![a-z0-9])graphflow(?![a-z0-9])", re.IGNORECASE)


def migration_inventory(codex, project, environment=None):
    listing = subprocess.run([codex, "mcp", "list", "--json"], cwd=project, capture_output=True,
                             encoding="utf-8", timeout=20, check=True, env=environment)
    rows = json.loads(listing.stdout)
    configured = {row["name"]: row["enabled"] for row in rows}
    candidates, unverified = {}, []
    for row in rows:
        name = row["name"]
        if name == "starwish":
            continue
        transport = row.get("transport") or {}
        identity = [name, transport.get("command"), *(transport.get("args") or [])]
        if isinstance(transport.get("url"), str):
            try:
                url = urlsplit(transport["url"])
                identity.extend([url.hostname, url.path])
            except ValueError:
                pass
        if any(isinstance(value, str) and _ENGINE_MARKER.search(value) for value in identity):
            candidates[name] = "enabled" if row["enabled"] else "disabled"
        elif row["enabled"]:
            unverified.append(name)
    return configured, {"starwish_configured": bool(configured.get("starwish")),
                        "graphflow_candidates": dict(sorted(candidates.items())),
                        "unverified_enabled_servers": sorted(unverified),
                        "scope": "effective_config_identity", "unrelated_servers_started": False}


@contextmanager
def loopback_model():
    captured = queue.Queue(maxsize=4)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not self.path.endswith("/responses") or not 0 < length <= 4 * 1024 * 1024:
                    self.send_error(400)
                    return
                present = HOOK_HINT in self.rfile.read(length).decode("utf-8")
                captured.put_nowait(present)
            except (UnicodeError, ValueError, queue.Full):
                self.send_error(400)
                return
            response = {"id": "resp_hook_probe", "object": "response", "status": "completed", "output": [],
                        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                                  "input_tokens_details": {"cached_tokens": 0},
                                  "output_tokens_details": {"reasoning_tokens": 0}}}
            wire = ("event: response.completed\ndata: " +
                    json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", captured
        finally:
            server.shutdown()
            worker.join(timeout=5)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify_hook(codex, project, workspace, *, isolate_other_hooks=False):
    suppressed = ()
    if isolate_other_hooks:
        readiness = probe(codex, project, workspace, hooks_only=True, capture_other_hook_keys=True)
        keys = readiness.pop("_other_hook_keys")
        if readiness["expected_hook_matches"]:
            suppressed = tuple(sorted(set(keys)))
    with loopback_model() as model:
        return probe(codex, project, workspace, hooks_only=True, local_model=model, suppressed_hooks=suppressed)


def project_session_start_hooks(all_hooks, project):
    config_path = project / ".codex" / "config.toml"
    return [hook for hook in all_hooks if hook["eventName"] == "sessionStart"
            and Path(hook["sourcePath"]).resolve() == config_path]


def hook_readiness(all_hooks, project, workspace):
    config_path = project / ".codex" / "config.toml"
    project_hooks = project_session_start_hooks(all_hooks, project)
    other_events = Counter(hook["eventName"] for hook in all_hooks if hook["enabled"] and hook not in project_hooks)
    hooks = [{"enabled": hook["enabled"], "trust": hook["trustStatus"]} for hook in project_hooks]
    commands = set()
    try:
        commands.add(_hook_command(workspace, project))
        if project == workspace:
            commands.add(_hook_command(workspace))
    except ValueError:
        pass
    matches = (len(project_hooks) == 1 and project_hooks[0].get("handlerType") == "command"
               and project_hooks[0].get("command") in commands
               and project_hooks[0].get("matcher") == "startup|resume" and not project_hooks[0].get("async"))
    return {"project_config": str(config_path), "project_session_start": hooks,
            "expected_hook_matches": matches,
            "other_enabled_hook_events": dict(sorted(other_events.items()))}


def explain_hook_trust(hook, layers):
    key = hook["key"]
    current = hook["currentHash"]
    normalized = key.replace("\\", "/").casefold()
    results = []
    for layer in layers:
        source = layer["name"]
        if source["type"] != "user":
            continue
        states = layer["config"].get("hooks", {}).get("state", {})
        states = states if isinstance(states, dict) else {}
        exact = states.get(key)
        definition_hash = exact.get("trusted_hash") if isinstance(exact, dict) else None
        path_variant = os.name == "nt" and any(
            other != key and other.replace("\\", "/").casefold() == normalized
            and isinstance(state.get("trusted_hash"), str)
            for other, state in states.items() if isinstance(state, dict)
        )
        if isinstance(definition_hash, str):
            status = "matching" if definition_hash == current else "stale"
        else:
            status = "missing"
        results.append({"file": source["file"], "profile": source.get("profile"),
                        "stored_definition_hash": status, "windows_key_variant_found": path_variant})
    return {"runtime_trust": hook["trustStatus"], "user_layers": results}


def verify_edit_preview(call, workspace, path, line):
    relative = Path(path)
    source = workspace / relative
    require(not relative.is_absolute() and source.resolve(strict=True).is_relative_to(workspace)
            and source.is_file(), "Preview target must be a file inside the workspace.")
    with source.open("rb") as handle:
        original = handle.read(512 * 1024 + 1)
    require(len(original) <= 512 * 1024, "Preview target is too large.")
    lines = original.splitlines(keepends=True)
    require(1 <= line <= len(lines), "Preview line is out of range.")
    raw = lines[line - 1]
    body = raw.rstrip(b"\r\n")
    expected = body.decode("utf-8-sig" if line == 1 else "utf-8")
    lines[line - 1] = body + b" " + raw[len(body):]
    result = json.loads(call("gf_edit", file=path, startLine=line, endLine=line, oldCode=expected,
                             newCode=expected + " ", rootDir=str(workspace), preview_only=True))
    require(result.get("path") == path and result.get("state") == "preview_only"
            and result.get("written") is False and result.get("queued") is False
            and "preview_id" not in result and "expires_in_seconds" not in result
            and result.get("base_sha256") == hashlib.sha256(original).hexdigest()
            and result.get("result_sha256") == hashlib.sha256(b"".join(lines)).hexdigest()
            and ("+" + expected + " \n") in result.get("diff", ""),
            "Edit preview did not return the expected diff and hashes without an approval receipt.")
    with source.open("rb") as handle:
        require(handle.read(len(original) + 1) == original, "Preview changed workspace bytes.")


def probe(codex, project, workspace, symbol=None, marker=None, *, hooks_only=False, project_only=False,
          local_model=None, environment=None, on_connected=None, caller_file=None, preview_line=None, startup_wait=35,
          stderr=subprocess.DEVNULL, capture_other_hook_keys=False, suppressed_hooks=(), explain_trust=False):
    configured, migration = migration_inventory(codex, project, environment)
    require(configured.get("starwish"), "StarWish is not enabled in this project's effective config.")
    require("enabled" not in migration["graphflow_candidates"].values(),
            "A possible independent GraphFlow server is still enabled; inspect with --config-only.")
    opts = []
    for name, enabled in configured.items():
        if enabled and (name != "starwish" or local_model is not None):
            # Codex validates each override as a complete transport before merging.
            opts.extend(["-c", f'mcp_servers.{json.dumps(name)}={{command="node",enabled=false}}'])
    if suppressed_hooks:
        entries = [f'{json.dumps(key, ensure_ascii=False)}={{enabled=false}}' for key in suppressed_hooks]
        require(len(entries) <= 80 and sum(map(len, entries)) <= 12000, "Too many hooks to isolate in one probe.")
        opts.extend(["-c", "hooks.state={" + ",".join(entries) + "}"])
    if local_model is not None:
        url, captured = local_model
        opts.extend(["-c", 'model_provider="starwish_hook_probe"', "-c", 'model="starwish-hook-probe"',
                     "-c", "analytics.enabled=false", "-c", "web_search=\"disabled\"", "-c",
                     f'model_providers.starwish_hook_probe={{name="StarWish loopback",base_url={json.dumps(url)},'
                     'wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}'])

    env = environment
    if local_model is not None:
        env = {key: value for key, value in (environment if environment is not None else os.environ).items() if key.upper() not in
               {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "RUST_LOG"}}
        env["NO_PROXY"] = "127.0.0.1,localhost"
    with subprocess.Popen([codex, *opts, "app-server", "--stdio"], cwd=project, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=stderr, encoding="utf-8") as process:
        responses = queue.Queue()

        def drain():
            for line in process.stdout:
                responses.put(line)
            responses.put(None)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        request_id = 0
        notifications = []

        def send(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def rpc(method, params, timeout=35):
            nonlocal request_id
            request_id += 1
            send({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    line = responses.get(timeout=max(.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if line is None:
                    break
                result = json.loads(line)
                if result.get("id") == request_id and "method" not in result:
                    require("error" not in result, f"Codex rejected {method}.")
                    return result["result"]
                if "method" in result and "id" not in result and local_model is not None:
                    notifications.append(result)
            raise RuntimeError(f"No response for {method}.")

        try:
            rpc("initialize", {"clientInfo": {"name": "starwish-probe", "version": "1"},
                               "capabilities": {"experimentalApi": True}})
            send({"method": "initialized"})
            if hooks_only or project_only:
                listed = rpc("hooks/list", {"cwds": [str(project)]})["data"]
                all_hooks = [hook for entry in listed for hook in entry["hooks"]]
                readiness = hook_readiness(all_hooks, project, workspace)
                hooks = readiness["project_session_start"]
                if local_model is not None:
                    report = {**readiness, "model_turn_started": False,
                              "memory_tools_called": False, "hint_injected": False}
                    if suppressed_hooks:
                        report["other_hook_isolation"] = {
                            "requested": len(suppressed_hooks),
                            "confirmed_disabled": len({h["key"] for h in all_hooks if not h["enabled"]}
                                                      & set(suppressed_hooks)),
                        }
                    if not readiness["expected_hook_matches"]:
                        return {**report, "status": "project_hook_definition_mismatch"}
                    if hooks != [{"enabled": True, "trust": "trusted"}]:
                        return {**report, "status": "project_hook_not_trusted"}
                    if readiness["other_enabled_hook_events"]:
                        return {**report, "status": "other_hooks_enabled"}
                    isolation = report.get("other_hook_isolation")
                    if isolation and isolation["requested"] != isolation["confirmed_disabled"]:
                        return {**report, "status": "other_hook_isolation_unconfirmed"}
                    started = rpc("thread/start", {"cwd": str(project), "ephemeral": True,
                                  "approvalPolicy": "never", "sandbox": "read-only"})
                    require(started.get("modelProvider") == "starwish_hook_probe",
                            "Codex did not select the loopback model provider.")
                    session = started["thread"]
                    require(session.get("ephemeral"), "Codex did not create a temporary session.")
                    thread = session["id"]
                    rpc("turn/start", {"threadId": thread, "input": [
                        {"type": "text", "text": "Local hook verification only.", "text_elements": []}]})
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        runs = [note["params"]["run"] for note in notifications
                                if note["method"] == "hook/completed" and note["params"]["threadId"] == thread]
                        completed = next((note["params"]["turn"] for note in notifications
                                          if note["method"] == "turn/completed"
                                          and note["params"]["threadId"] == thread), None)
                        if completed is not None:
                            require(completed["status"] == "completed", "Local hook verification turn failed.")
                            require(any(run["eventName"] == "sessionStart" and run["status"] == "completed"
                                        and Path(run["sourcePath"]).resolve() == Path(readiness["project_config"])
                                        for run in runs), "Project SessionStart hook did not complete.")
                            try:
                                require(captured.get(timeout=2), "The hook hint did not reach the loopback model.")
                            except queue.Empty as exc:
                                raise RuntimeError("No loopback model request received.") from exc
                            return {**report, "status": "verified", "model_turn_started": True, "hint_injected": True}
                        try:
                            line = responses.get(timeout=max(.01, deadline - time.monotonic()))
                        except queue.Empty:
                            break
                        if line is None:
                            break
                        note = json.loads(line)
                        if "method" in note and "id" not in note:
                            notifications.append(note)
                    raise RuntimeError("Timed out waiting for loopback hook verification.")
                if hooks_only:
                    own = project_session_start_hooks(all_hooks, project)
                    if capture_other_hook_keys:
                        readiness["_other_hook_keys"] = [h["key"] for h in all_hooks if h["enabled"] and h not in own]
                    if explain_trust:
                        if readiness["expected_hook_matches"]:
                            config_layers = rpc("config/read", {"cwd": str(project), "includeLayers": True})
                            readiness["hook_trust_storage"] = explain_hook_trust(own[0], config_layers.get("layers") or [])
                        else:
                            readiness["hook_trust_storage"] = {"status": "project_hook_definition_mismatch"}
                    return {"starwish_configured": True, **readiness, "model_turn_started": False}
            session = rpc("thread/start", {"cwd": str(project), "ephemeral": True}, timeout=startup_wait)["thread"]
            require(session.get("ephemeral"), "Codex did not create a temporary session.")
            scope = {"threadId": session["id"]}
            deadline = time.monotonic() + startup_wait
            while True:
                statuses = rpc("mcpServerStatus/list", {**scope, "detail": "toolsAndAuthOnly"}, timeout=startup_wait)
                states = {row["name"]: row for row in statuses["data"]}
                starwish = states.get("starwish", {})
                if starwish.get("runtimeStatus") == "connected" or time.monotonic() >= deadline:
                    break
                time.sleep(.2)
            require(starwish.get("runtimeStatus") == "connected", "StarWish did not connect.")
            for name in LEGACY:
                state = states.get(name)
                require(state is None or (state.get("runtimeStatus") == "disabled" and not state.get("tools")),
                        "An independent GraphFlow server remains callable.")
            tools = set(starwish["tools"])
            require(REQUIRED <= tools and not APPROVAL_ONLY & tools, "The StarWish tool surface differs from expected.")
            if project_only:
                missing = sorted(COMPAT - tools)
                require(not missing, "Missing compatibility tools: " + ", ".join(missing))

            def call(tool, **arguments):
                result = rpc("mcpServer/tool/call", {**scope, "server": "starwish", "tool": tool,
                                                     "arguments": arguments}, timeout=60)
                require(not result.get("isError"), f"StarWish tool {tool} failed.")
                return "\n".join(item["text"] for item in result["content"] if item["type"] == "text")

            if on_connected is not None:
                return on_connected(call)

            checks = ["context-anchor", "source-range", "approval-boundary"]
            if not project_only:
                index = call("recall_index")
                core = call("get_core")
                require(bool(index.strip()) and marker in core, "The configured test memory did not round-trip.")
                checks += ["core-marker", "memory-index"]
            path, name = symbol.split("#", 1)
            context = json.loads(call("graphflow_context", query=name, rootDir=str(workspace)))
            anchors = [item for item in context.get("anchors", []) if item.get("file") == path]
            require(bool(anchors), "Project context did not return an anchor for the expected file.")
            for anchor in anchors:
                result = json.loads(call("graphflow_read", target=anchor["read_target"]))
                if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", result.get("text", "")):
                    break
            else:
                raise RuntimeError("No returned anchor expanded to the expected symbol.")
            report = {"starwish": "connected", "legacy": {name: states.get(name, {}).get("runtimeStatus", "absent")
                                                          for name in sorted(LEGACY)}, "checks": checks,
                      "migration_inventory": migration}
            if project_only:
                if preview_line is not None:
                    verify_edit_preview(call, workspace, path, preview_line)
                    checks.append("edit-diff-without-receipt")
                impact = json.loads(call("graphflow_impact", target=symbol))
                matching = [item for item in impact["impacts"] if item.get("symbol") == name]
                require(bool(matching), "Static impact did not identify the expected symbol.")
                checks.append("impact-symbol")
                if caller_file:
                    require(any(site.get("file") == caller_file and type(site.get("line")) is int and site["line"] > 0
                                for item in matching for site in item["call_sites"]),
                            "Static impact did not locate the expected caller.")
                    checks.append("impact-caller")
                workflow = json.loads(call("graphflow_workflow_preview", name="symbol-review", target=symbol))
                plan = json.loads(call("graphflow_plan", task="Plan a possible change to the probe symbol",
                                       target=symbol, intent="change", rootDir=str(workspace)))
                steps = {step["id"]: (step["gate"], step["dependsOn"]) for step in plan["plan"]["steps"]}
                require(plan.get("intent") == "change" and plan.get("executed") is False
                        and plan.get("persisted") is False
                        and any(anchor.get("file") == path for anchor in plan["context"]["anchors"])
                        and steps.get("preview") == ("read_only", ["impact"])
                        and steps.get("apply") == ("operator_approval", ["preview"])
                        and steps.get("test") == ("operator_approval", ["apply"])
                        and steps.get("rollback") == ("conditional_operator_approval", ["test"])
                        and {"from": "rollback", "to": "report", "condition": "verification_failed"}
                        in plan["plan"]["dag"]["edges"]
                        and plan.get("read_only_workflow") == {"name": "symbol-review", "target": symbol,
                                                               "plan_sha256": workflow["plan_sha256"]},
                        "Change planning did not separate approval gates from the read-only replay receipt.")
                checks.append("change-plan-approval-gates")
                macro_args = {"id": "symbol-review", "args": [symbol], "rootDir": str(workspace)}
                macro = json.loads(call("gf_macro", action="preview", **macro_args))
                ops = ["read", "impact", "test_plan"]
                require(workflow.get("read_only") is True and [step["op"] for step in workflow["steps"]] == ops
                        and macro.get("state") == "preview" and macro.get("steps") == workflow["steps"]
                        and macro.get("plan_sha256") == workflow.get("plan_sha256")
                        and macro.get("files_written") is False and macro.get("test_executed") is False,
                        "Workflow and macro previews did not agree on the read-only plan.")
                replay = json.loads(call("gf_macro", action="run", **macro_args, plan_sha256=workflow["plan_sha256"]))
                require(replay.get("state") == "completed"
                        and [step["op"] for step in replay["steps"]] == ops
                        and all(step.get("state") == "completed" for step in replay["steps"])
                        and replay.get("files_written") is False and replay.get("test_executed") is False
                        and json.loads(replay["steps"][-1]["text"]).get("executed") is False,
                        "Read-only macro replay did not complete without running tests or writing files.")
                checks += ["workflow-preview", "read-only-macro-replay"]
                diagnostic = json.loads(call("graphflow_diagnose", rootDir=str(workspace)))
                require(diagnostic.get("engine") == "ready" and diagnostic.get("read_only"), "Project diagnostic did not confirm readiness.")
                doctor = call("gf_exec", cmd="doctor", rootDir=str(workspace))
                require(doctor.startswith("PASS [graphflow-status]") and "Index: " in doctor
                        and "effective registration not checked" in doctor and "Tasks: " in doctor,
                        "Project doctor did not report scoped diagnostics.")
                checks.append("project-doctor")
                guide = json.loads(call("graphflow_skill_guide", rootDir=str(workspace)))
                require(guide.get("server") == "starwish", "Project guide did not identify StarWish.")
                brief = call("gf_read", targets=["@brief"], rootDir=str(workspace))
                require("Cross-Agent Project Onboarding & Handoff Brief" in brief and "星愿项目交接" in brief,
                        "Cross-agent project brief did not include both index and handoff sections.")
                insight_args = {"task": "Review the probe symbol", "target": symbol, "rootDir": str(workspace)}
                draft = json.loads(call("graphflow_insight", **insight_args))
                reviewed = json.loads(call("graphflow_insight", **insight_args, plan_sha256=draft["plan_sha256"],
                                           observations=[{"step": "locate", "note": "Probe: review the source range."}]))
                require(reviewed.get("plan") == draft.get("plan") and reviewed.get("state") == "reviewed"
                        and reviewed.get("executed") is False and reviewed.get("approved") is False,
                        "Project insight did not preserve plan boundaries.")
                exported = json.loads(call("graphflow_artifact", mode="export", rootDir=str(workspace)))
                require(exported.get("artifact") and exported.get("workspace_written") is False,
                        "Index manifest export did not pass.")
                graph = json.loads(call("graphflow_graph", target=path, nodeLimit=8, edgeLimit=12, rootDir=str(workspace)))
                require(any(node.get("file") == path and node.get("kind") == "file" for node in graph["nodes"])
                        and len(graph["nodes"]) <= 8 and len(graph["edges"]) <= 12, "Code graph did not match the target or limits.")
                reply = rpc("mcpServer/tool/call", {**scope, "server": "starwish", "tool": "gf_exec",
                                                    "arguments": {"cmd": "affected-tests", "run": True}})
                require(reply.get("isError") is True, "Unapproved test execution was not rejected.")
                checks += ["compatibility-tools", "diagnostics", "skill-guide", "handoff-brief", "insight-review",
                           "manifest-export", "code-graph", "unapproved-test-rejected"]
                report.update(project_session_start=hooks, model_turn_started=False, memory_tools_called=False)
            return report
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, type=Path, help="Path to the Codex binary used by the client.")
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--symbol", help="Known relative path#symbol.")
    parser.add_argument("--caller-file", help="Known relative to the workspace; requires a static call site in project-only mode.")
    parser.add_argument("--preview-line", type=int, help="Line to preview in memory without an approval receipt (project-only).")
    parser.add_argument("--startup-wait", type=int, default=35, choices=range(1, 181), metavar="1..180",
                        help="Seconds to wait for MCP startup, including cold single-file extraction.")
    parser.add_argument("--fixture-marker", help="Marker seeded into a disposable test soul only.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--hooks-only", action="store_true", help="Read hook status without opening a session or MCP connection.")
    modes.add_argument("--verify-hook", action="store_true", help="Check an already trusted hook using a loopback mock model.")
    parser.add_argument("--isolate-other-hooks", action="store_true",
                        help="With --verify-hook, temporarily disable other discovered hooks only in the probe process.")
    parser.add_argument("--explain-hook-trust", action="store_true",
                        help="With --hooks-only, report whether the exact hook definition is trusted in the user config.")
    modes.add_argument("--project-only", action="store_true", help="Exercise project tools without calling memory tools.")
    modes.add_argument("--config-only", action="store_true", help="Inspect effective registrations without starting MCP.")
    args = parser.parse_args()
    if args.isolate_other_hooks and not args.verify_hook:
        parser.error("--isolate-other-hooks requires --verify-hook.")
    if args.explain_hook_trust and not args.hooks_only:
        parser.error("--explain-hook-trust requires --hooks-only.")
    inspection_mode = args.hooks_only or args.verify_hook or args.config_only
    if inspection_mode and (args.symbol is not None or args.fixture_marker is not None):
        parser.error("Configuration and hook inspection do not accept fixture arguments.")
    if not inspection_mode and (not args.symbol or "#" not in args.symbol or not all(args.symbol.split("#", 1))):
        parser.error("Provide a relative path#symbol.")
    if args.project_only and args.fixture_marker is not None:
        parser.error("Project-only mode does not accept a fixture marker.")
    if args.caller_file is not None and (not args.project_only or not args.caller_file.strip()):
        parser.error("Caller file requires project-only mode and a nonempty relative path.")
    if args.preview_line is not None and (not args.project_only or args.preview_line < 1):
        parser.error("Preview line requires project-only mode and a positive line number.")
    if not (inspection_mode or args.project_only or (args.fixture_marker and args.fixture_marker.strip())):
        parser.error("Provide a nonempty fixture marker or select project-only mode.")
    try:
        paths = (str(args.codex.resolve(strict=True)), args.project.resolve(strict=True), args.workspace.resolve(strict=True))
        if args.config_only:
            _, result = migration_inventory(*paths[:2])
        elif args.verify_hook:
            result = verify_hook(*paths, isolate_other_hooks=args.isolate_other_hooks)
        else:
            result = probe(*paths, args.symbol, args.fixture_marker, hooks_only=args.hooks_only,
                           project_only=args.project_only, caller_file=args.caller_file, preview_line=args.preview_line,
                           startup_wait=args.startup_wait, explain_trust=args.explain_hook_trust)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Probe failed: {exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
