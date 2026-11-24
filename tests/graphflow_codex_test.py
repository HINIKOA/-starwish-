"""Opt-in Codex migration verification with a disposable CODEX_HOME."""
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, graphflow, graphflow_workspace_check, graphflow_workspaces
from app.graphflow_routes import router
from tools import graphflow_codex, graphflow_codex_probe

_COMPACT_SUMMARY = "The Juniper Atlas verifies translations by blind peer review."
_REVIEW_SKILL = "check-source-numbering"
_REJECT_SKILL = "check-page-headers"
_SKILL_USAGE = "SKILL_APPLICATION_FIXTURE: Check citations 1 and 3 against sources 1 and 2."
_AUTO_TRIGGER_SAMPLE = "AUTO_COMPACT_FIXTURE: compare peer review against the citation log."


@contextmanager
def local_model():
    requests = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(body)
            requests.put((self.path, request))
            response = {
                "id": "resp_fixture", "object": "response", "created_at": 1, "status": "completed",
                "model": "starwish-fixture", "output": [],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}},
            }
            if self.path.endswith("/chat/completions"):
                if any(m.get("content", "").startswith("Review this Codex conversation") for m in request["messages"]):
                    content = json.dumps({"skills": [{"action": "create", "name": _REVIEW_SKILL,
                        "content": f"---\nname: {_REVIEW_SKILL}\ndescription: Validate numbered citations\n---\n"
                                   "# Procedure\nAudit source numbering before finalizing a manuscript."},
                        {"action": "create", "name": _REJECT_SKILL,
                         "content": f"---\nname: {_REJECT_SKILL}\ndescription: Validate manuscript page headings\n---\n"
                                    "# Procedure\nCheck each page heading against the final manuscript source."}]})
                else:
                    content = json.dumps({"items": [{"kind": "fact", "text": _COMPACT_SUMMARY}]})
                wire = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
                return
            if self.path.endswith("/responses/compact"):
                response.update(object="response.compaction", output=[
                    {"id": "cmp_fixture", "type": "compaction", "encrypted_content": "fixture"}])
                wire = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
                return
            user_inputs = [item["content"] for item in request.get("input", [])
                           if isinstance(item, dict) and item.get("role") == "user" and "content" in item]
            if user_inputs and _AUTO_TRIGGER_SAMPLE in json.dumps(user_inputs[-1]):
                response["usage"].update(input_tokens=9000, output_tokens=2, total_tokens=9002)
            answer, fixture_call = "Fixture response.", None
            outputs = [item for item in request.get("input", []) if isinstance(item, dict)
                       and item.get("type") == "function_call_output" and item.get("call_id") == "fixture_skill_read"]
            if outputs:
                if "Audit source numbering before finalizing" in json.dumps(outputs):
                    answer = "Citation 3 has no source entry."
            elif user_inputs and _SKILL_USAGE in json.dumps(user_inputs[-1]):
                for tool in request.get("tools", []):
                    name = tool.get("name", "")
                    if name == "mcp__starwish__skill_view":
                        fixture_call = {"name": name}
                    elif tool.get("type") == "namespace" and "starwish" in name and any(
                        definition.get("name") == "skill_view" for definition in tool.get("tools", [])
                    ):
                        fixture_call = {"name": "skill_view", "namespace": name}
                    if fixture_call:
                        break
            message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": answer, "annotations": []}]}
            if fixture_call:
                message = {"id": "fc_fixture", "type": "function_call", "status": "completed",
                           "call_id": "fixture_skill_read", "arguments": json.dumps({"name": _REVIEW_SKILL}),
                           **fixture_call}
            response["output"].append(message)
            events = [
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": response},
            ]
            wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
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
            yield f"http://127.0.0.1:{server.server_port}/v1", requests
        finally:
            server.shutdown()
            worker.join(timeout=5)


class CodexProfileTest(unittest.TestCase):
    def test_site_preflight_reports_missing_prerequisites_without_actions(self):
        inventory = {"starwish_configured": True, "graphflow_candidates": {}, "unrelated_servers_started": False}
        hook = {"expected_hook_matches": True, "project_session_start": [{"enabled": True, "trust": "untrusted"}]}
        with (patch.object(graphflow_codex_probe, "migration_inventory", return_value=({}, inventory)),
              patch.object(graphflow_codex_probe, "panel_status", return_value={"status": "unreachable"}),
              patch.object(graphflow_codex_probe, "probe", return_value=hook) as inspect_hook):
            report = graphflow_codex_probe.site_preflight("codex", Path("."), Path("."), "http://127.0.0.1:17860")
        self.assertEqual(report["actions"], ["review_and_trust_project_hook", "start_or_check_local_panel"])
        self.assertFalse(report["mcp_started"] or report["model_turn_started"] or report["operator_actions_performed"])
        inspect_hook.assert_called_once_with("codex", Path("."), Path("."), hooks_only=True, explain_trust=True)

    def test_panel_status_only_requests_fixed_local_json_and_does_not_follow_redirects(self):
        urls = []
        status = {"workspace": "fixture", "available": True, "edit_queue": True, "test_enabled": False,
                  "operator_key_configured": False, "extra": "do-not-report"}
        expected = Path("fixture")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                urls.append(self.path)
                if len(urls) > 1:
                    self.send_response(302)
                    self.send_header("Location", "https://outside.invalid")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                claim = self.headers.get(graphflow_workspace_check.HEADER, "")
                check = "matched" if graphflow_workspace_check.matches(expected, claim) else "mismatch"
                self.wfile.write(json.dumps({**status, "workspace_check": check}).encode())

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                report = graphflow_codex_probe.panel_status(url, Path("fixture"))
                self.assertEqual(report, {"status": "reachable", "workspace_name_matches": True, "available": True,
                                          "workspace_check": "matched",
                                          "edit_queue": True, "test_enabled": False, "operator_key_configured": False})
                self.assertEqual(graphflow_codex_probe.panel_status(url, Path("fixture")),
                                 {"status": "unexpected_http", "http_status": 302})
                for invalid in ("http://example.invalid:80", "http://127.0.0.1", url + "/custom", url + "?q=one"):
                    with self.assertRaises(ValueError):
                        graphflow_codex_probe.panel_status(invalid, Path("fixture"))
                self.assertEqual(urls, ["/api/graphflow/status"] * 2)
            finally:
                server.shutdown()
                worker.join(timeout=5)

    def test_panel_rejects_same_named_git_project_and_only_reports_match(self):
        with tempfile.TemporaryDirectory() as root:
            expected = Path(root, "one", "fixture").resolve()
            other = Path(root, "two", "fixture").resolve()
            for repo in (expected, other):
                (repo / ".git").mkdir(parents=True)
            ready = FastAPI()
            ready.include_router(router)
            endpoint = "/api/graphflow/status"
            claim = graphflow_workspace_check.new_claim(expected)
            headers = {graphflow_workspace_check.HEADER: claim}
            self.assertNotIn(str(expected), claim)
            self.assertNotEqual(claim, graphflow_workspace_check.new_claim(expected))
            with TestClient(ready, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as client:
                for repo, verdict in ((expected, "matched"), (other, "mismatch")):
                    with self.subTest(repo=repo), patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(repo)}):
                        response = client.get(endpoint, headers=headers)
                        self.assertEqual(response.json()["workspace"], expected.name)
                        self.assertEqual(response.json()["workspace_check"], verdict)
                        self.assertNotIn(claim, response.text)
                        self.assertNotIn(str(repo), response.text)
                        self.assertTrue("workspace_check" not in client.get(endpoint).json())
                        self.assertEqual(client.get(endpoint, headers={
                            **headers, "Origin": "http://elsewhere.invalid"}).status_code, 403)
            inventory = {"starwish_configured": True, "graphflow_candidates": {}}
            hook = {"expected_hook_matches": True, "project_session_start": [{"enabled": True, "trust": "trusted"}]}
            for verdict in ("mismatch", "unsupported", "matched"):
                panel = {"status": "reachable", "available": True, "workspace_name_matches": True,
                         "workspace_check": verdict, "edit_queue": True, "test_enabled": True,
                         "operator_key_configured": True}
                with (patch.object(graphflow_codex_probe, "migration_inventory", return_value=({}, inventory)),
                      patch.object(graphflow_codex_probe, "panel_status", return_value=panel),
                      patch.object(graphflow_codex_probe, "probe", return_value=hook)):
                    result = graphflow_codex_probe.site_preflight("codex", expected, expected, "http://127.0.0.1:17860")
                if verdict == "matched":
                    self.assertEqual(result["actions"], [])
                    self.assertEqual(result["status"], "ready_for_operator_verification")
                    self.assertFalse(result["operator_actions_performed"])
                else:
                    self.assertEqual(result["actions"], ["check_panel_engine_and_workspace"])
                    self.assertEqual(result["status"], "needs_setup")

    def test_migration_inventory_uses_only_identity_fields(self):
        rows = [
            {"name": "starwish", "enabled": True, "transport": {"args": ["graphflow"]}},
            {"name": "coding-helper", "enabled": True, "transport": {"args": ["@roarpeng/graphflow@3.9.2"]}},
            {"name": "old-code", "enabled": False, "transport": {"command": r"C:\tools\graphflow.exe"}},
            {"name": "remote", "enabled": True, "transport": {"url": "http://example.test/graphflow/mcp"}},
            {"name": "opaque", "enabled": True, "transport": {"url": "http://example.test/mcp?token=graphflow",
                                                             "env": {"SECRET": "do-not-report"}}},
        ]
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows))
        with patch.object(graphflow_codex_probe.subprocess, "run", return_value=result) as listing:
            _, report = graphflow_codex_probe.migration_inventory("codex", ".")
            self.assertEqual(report["graphflow_candidates"],
                             {"coding-helper": "enabled", "old-code": "disabled", "remote": "enabled"})
            self.assertEqual(report["unverified_enabled_servers"], ["opaque"])
            self.assertNotIn("do-not-report", json.dumps(report))
            self.assertTrue(report["starwish_configured"])
            self.assertFalse(report["unrelated_servers_started"])
            listing.assert_called_once()
            with patch.object(graphflow_codex_probe.subprocess, "Popen") as start:
                with self.assertRaisesRegex(RuntimeError, "independent GraphFlow"):
                    graphflow_codex_probe.probe("codex", ".", ".")
                start.assert_not_called()

    def test_hook_probe_identifies_exact_project_hook_without_disclosing_other_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            workspace = project / "repo"
            path = project / ".codex" / "config.toml"
            own = {"eventName": "sessionStart", "sourcePath": str(path), "enabled": True,
                   "trustStatus": "trusted", "handlerType": "command", "matcher": "startup|resume",
                   "command": graphflow_codex._hook_command(workspace, project)}
            other = {**own, "sourcePath": str(project / "hooks.json"), "command": "private-command"}
            readiness = graphflow_codex_probe.hook_readiness([own, other], project, workspace)
            self.assertTrue(readiness["expected_hook_matches"])
            self.assertEqual(readiness["project_config"], str(path))
            self.assertEqual(readiness["other_enabled_hook_events"], {"sessionStart": 1})
            self.assertNotIn("private-command", json.dumps(readiness))
            for change in ({"command": "unexpected"}, {"matcher": "resume"}, {"async": True}):
                readiness = graphflow_codex_probe.hook_readiness([{**own, **change}], project, workspace)
                self.assertFalse(readiness["expected_hook_matches"])

    def test_hook_trust_diagnostics_do_not_disclose_hashes_or_non_user_layers(self):
        key = r"D:\repo\.codex\config.toml:session_start:0:0"
        hook = {"key": key, "currentHash": "private-current", "trustStatus": "untrusted"}
        source = {"type": "user", "file": "config.toml", "profile": None}
        project = {"name": {"type": "project"}, "config": {"hooks": {"state": {
            key: {"trusted_hash": "private-current"}}}}}
        variants = {key.lower(): {"trusted_hash": "private-variant"}}
        for value, expected in ((None, "missing"), ("private-old", "stale"), ("private-current", "matching")):
            states = variants.copy()
            if value:
                states[key] = {"trusted_hash": value}
            user = {"name": source, "config": {"hooks": {"state": states}}}
            report = graphflow_codex_probe.explain_hook_trust(hook, [project, user])
            self.assertEqual(report["runtime_trust"], "untrusted")
            self.assertEqual(report["user_layers"], [{
                "file": "config.toml", "profile": None, "stored_definition_hash": expected,
                "windows_key_variant_found": os.name == "nt",
            }])
            self.assertNotIn("private-", json.dumps(report))

    def test_managed_install_never_overwrites_or_removes_changed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, soul = root / "project", root / "soul"
            repo = project / "repo"
            (repo / ".git").mkdir(parents=True)
            soul.mkdir()
            layer = project / ".codex" / "config.toml"
            receipt = graphflow_codex.install(project, repo, soul, include_hook=True)
            original = layer.read_bytes()
            self.assertIn(b"--client-root", original)
            self.assertNotIn(b"STARWISH_GRAPHFLOW_OPERATOR_KEY", original)
            self.assertNotIn(b"STARWISH_GRAPHFLOW_EDIT_QUEUE", original)
            with self.assertRaises(ValueError):
                graphflow_codex.install(project, repo, soul)
            self.assertEqual(layer.read_bytes(), original)
            layer.write_bytes(original + b"# user change\n")
            with self.assertRaises(ValueError):
                graphflow_codex.uninstall(project, repo, receipt["sha256"])
            self.assertTrue(layer.read_bytes().endswith(b"# user change\n"))
            layer.write_bytes(original)
            with self.assertRaises(ValueError):
                graphflow_codex.uninstall(project, repo, "0" * 64)
            self.assertEqual(graphflow_codex.uninstall(project, repo, receipt["sha256"])["status"], "removed")
            self.assertFalse(layer.exists())
            graphflow_codex.install(project, repo, soul, edit_queue=True)
            self.assertIn(b'STARWISH_GRAPHFLOW_EDIT_QUEUE = "1"', layer.read_bytes())
            self.assertNotIn(b"STARWISH_GRAPHFLOW_OPERATOR_KEY", layer.read_bytes())

    def test_hook_uses_only_event_and_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            command = [sys.executable, str(graphflow_codex.ROOT / "tools" / "graphflow_codex.py"),
                       "session-hook", "--workspace", str(repo)]
            base = {"hook_event_name": "SessionStart", "source": "startup",
                    "transcript_path": str(repo / "does-not-exist"), "prompt": "never echo this"}
            result = subprocess.run(command, input=json.dumps({**base, "cwd": str(repo)}), text=True,
                                    capture_output=True, check=True)
            self.assertIn("graphflow_context", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])
            self.assertNotIn("never echo this", result.stdout)
            for change in ({"cwd": str(root)}, {"cwd": "."}, {"source": []}, {"hook_event_name": "Stop"}):
                self.assertIsNone(graphflow_codex.session_hint(repo, {**base, "cwd": str(repo), **change}))
            allowed = subprocess.run([*command, "--client-root", str(root)],
                                     input=json.dumps({**base, "cwd": str(root)}), text=True,
                                     capture_output=True, check=True)
            self.assertIn("graphflow_context", allowed.stdout)
            sibling = root / "sibling"
            sibling.mkdir()
            for denied in (sibling, root.parent):
                self.assertIsNone(graphflow_codex.session_hint(repo, {**base, "cwd": str(denied)}, root))
            for invalid in (sibling, Path(root.anchor), Path("relative")):
                with self.assertRaises(ValueError):
                    graphflow_codex.preview(repo, root / "soul", include_hook=True, client_root=invalid)

    def test_preview_does_not_write_client_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            before = set(root.iterdir())
            plan = graphflow_codex.preview(repo, root / "soul", disable_server="graphflow", tasks=True)
            self.assertIn("[mcp_servers.starwish]", plan)
            self.assertIn("[mcp_servers.graphflow]\nenabled = false", plan)
            self.assertNotIn("STARWISH_GRAPHFLOW_OPERATOR_KEY", plan)
            self.assertEqual(set(root.iterdir()), before)
            with self.assertRaises(ValueError):
                graphflow_codex.preview(repo, root / "soul", disable_server="unrelated")
            with self.assertRaises(ValueError):
                graphflow_codex.preview(repo, repo)

    def test_alias_selection_is_explicit_and_rejects_unverified_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, soul, executable = root / "repo", root / "soul", root / "codex.exe"
            (repo / ".git").mkdir(parents=True)
            soul.mkdir()
            executable.touch()
            with patch.object(graphflow_codex_probe, "migration_inventory", return_value=(
                    {}, {"graphflow_candidates": {"code-index": "enabled", "old-index": "disabled"}}
            )) as scan:
                for bad in ("starwish", "STARWISH", "outside]", "code-index\n[mcp_servers.other"):
                    with self.subTest(alias=bad), self.assertRaisesRegex(ValueError, "distinct"):
                        graphflow_codex.preview(repo, soul, project=repo, codex=executable, disable_aliases=(bad,))
                scan.assert_not_called()
                for bad in ("unrelated", "old-index"):
                    with self.subTest(alias=bad), self.assertRaisesRegex(ValueError, "not an enabled"):
                        graphflow_codex.install(repo, repo, soul, codex=executable, disable_aliases=(bad,))
                preview = graphflow_codex.preview(repo, soul, project=repo, codex=executable,
                                                  disable_aliases=("code-index",))
                self.assertIn("[mcp_servers.code-index]\nenabled = false", preview)
                self.assertNotIn("[mcp_servers.old-index]", preview)
                self.assertFalse((repo / ".codex").exists())

    @unittest.skipUnless(os.environ.get("STARWISH_GRAPHFLOW_CODEX_TEST") == "1", "optional Codex CLI integration")
    def test_real_codex_profile_is_reversible(self):
        codex = shutil.which("codex.cmd" if os.name == "nt" else "codex")
        self.assertIsNotNone(codex)
        with tempfile.TemporaryDirectory(prefix="starwish-codex-") as directory:
            root = Path(directory).resolve()
            repo, home = root / "repo", root / "codex-home"
            (repo / ".git").mkdir(parents=True)
            home.mkdir()
            base = home / "config.toml"
            original = '[mcp_servers.graphflow]\ncommand = "node"\nargs = ["legacy-server.js"]\n'
            base.write_text(original, encoding="utf-8")
            layer = home / "starwish-test.config.toml"
            layer.write_text(graphflow_codex.preview(repo, root / "soul", disable_server="graphflow",
                                                     include_hook=True), encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith("CODEX_")}
            env["CODEX_HOME"] = str(home)

            def listing(*opts):
                result = subprocess.run([codex, *opts, "mcp", "list", "--json"], cwd=repo, env=env, check=True,
                                        capture_output=True, encoding="utf-8", timeout=20)
                return {row["name"]: row["enabled"] for row in json.loads(result.stdout)}

            self.assertEqual(listing(), {"graphflow": True})
            self.assertEqual(listing("-p", "starwish-test"), {"graphflow": False, "starwish": True})
            self.assertEqual(listing(), {"graphflow": True})
            self.assertEqual(base.read_text(encoding="utf-8"), original)

    @unittest.skipUnless(os.environ.get("STARWISH_GRAPHFLOW_CODEX_TEST") == "1", "optional Codex CLI integration")
    def test_real_codex_project_layer_is_scoped_and_reversible(self):
        codex = shutil.which("codex.cmd" if os.name == "nt" else "codex")
        self.assertIsNotNone(codex)
        with tempfile.TemporaryDirectory(prefix="starwish-codex-layer-") as directory:
            root = Path(directory).resolve()
            repo, other, home = root / "repo", root / "other", root / "codex-home"
            for project in (repo, other):
                (project / ".git").mkdir(parents=True)
            home.mkdir()
            base = home / "config.toml"
            original = ('[mcp_servers.graphflow]\ncommand = "node"\nargs = ["legacy-server.js"]\n'
                        f'[projects.{graphflow_codex._toml(str(repo))}]\ntrust_level = "trusted"\n'
                        f'[projects.{graphflow_codex._toml(str(other))}]\ntrust_level = "untrusted"\n')
            base.write_bytes(original.encode("utf-8"))
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith("CODEX_")}
            env["CODEX_HOME"] = str(home)

            def listing(cwd):
                result = subprocess.run([codex, "mcp", "list", "--json"], cwd=cwd, env=env, check=True,
                                        capture_output=True, encoding="utf-8", timeout=20)
                return {row["name"]: row["enabled"] for row in json.loads(result.stdout)}

            self.assertEqual(listing(repo), {"graphflow": True})
            soul = root / "soul"
            soul.mkdir()
            receipts = {project: graphflow_codex.install(project, project, soul, disable_server="graphflow",
                                                         include_hook=True) for project in (repo, other)}
            self.assertEqual(listing(repo), {"graphflow": False, "starwish": True})
            self.assertEqual(listing(other), {"graphflow": True})
            alias = '\n[mcp_servers.code-index]\ncommand = "node"\nargs = ["@roarpeng/graphflow@3.9.2"]\n'
            base.write_bytes((original + alias).encode("utf-8"))
            _, inventory = graphflow_codex_probe.migration_inventory(codex, repo, env)
            self.assertEqual(inventory["graphflow_candidates"], {"code-index": "enabled", "graphflow": "disabled"})
            with self.assertRaisesRegex(RuntimeError, "independent GraphFlow"):
                graphflow_codex_probe.probe(codex, repo, repo, hooks_only=True, environment=env)
            base.write_bytes((original + alias + 'enabled = false\n').encode("utf-8"))
            _, inventory = graphflow_codex_probe.migration_inventory(codex, repo, env)
            self.assertEqual(inventory["graphflow_candidates"], {"code-index": "disabled", "graphflow": "disabled"})
            base.write_bytes(original.encode("utf-8"))
            with patch.dict(os.environ, env, clear=True):
                hooks = graphflow_codex_probe.probe(codex, repo, repo, hooks_only=True)
                untrusted = graphflow_codex_probe.verify_hook(codex, repo, repo)
            self.assertEqual(hooks["project_session_start"], [{"enabled": True, "trust": "untrusted"}])
            self.assertFalse(hooks["model_turn_started"])
            self.assertEqual(untrusted["status"], "project_hook_not_trusted")
            self.assertFalse(untrusted["model_turn_started"])
            graphflow_codex.uninstall(repo, repo, receipts[repo]["sha256"])
            self.assertEqual(listing(repo), {"graphflow": True})
            self.assertEqual(base.read_bytes(), original.encode("utf-8"))
            global_data = (original + alias + '\n[mcp_servers.keep]\ncommand = "node"\n').encode("utf-8")
            base.write_bytes(global_data)
            public = {"graphflow": True, "code-index": True, "keep": True}
            self.assertEqual(listing(repo), public)
            binary = Path(os.environ.get("STARWISH_GRAPHFLOW_CODEX_BINARY") or codex).resolve()
            with self.assertRaisesRegex(ValueError, "not an enabled GraphFlow candidate"):
                graphflow_codex.install(repo, repo, soul, codex=binary, disable_aliases=("keep",), environment=env)
            cli = [sys.executable, graphflow_codex.__file__, "install", "--project", str(repo), "--workspace", str(repo),
                   "--soul", str(soul), "--codex", str(binary), "--disable-server", "graphflow",
                   "--disable-alias", "code-index"]
            migrated = json.loads(subprocess.run(cli, cwd=repo, env=env, capture_output=True,
                                                encoding="utf-8", timeout=25, check=True).stdout)
            self.assertEqual(listing(repo), {"graphflow": False, "code-index": False, "keep": True, "starwish": True})
            self.assertEqual(listing(other), public)
            self.assertEqual(base.read_bytes(), global_data)
            graphflow_codex.uninstall(repo, repo, migrated["sha256"])
            self.assertEqual(listing(repo), public)
            self.assertEqual(base.read_bytes(), global_data)

    @unittest.skipUnless(os.environ.get("STARWISH_GRAPHFLOW_CODEX_TEST") == "1", "optional Codex app-server integration")
    def test_real_codex_calls_starwish_without_legacy_server(self):
        codex = os.environ.get("STARWISH_GRAPHFLOW_CODEX_BINARY") or shutil.which("codex.cmd" if os.name == "nt" else "codex")
        self.assertIsNotNone(codex)
        with tempfile.TemporaryDirectory(prefix="starwish-codex-rpc-") as directory, \
                tempfile.TemporaryFile() as stderr, local_model() as (model_url, model_requests):
            root = Path(directory).resolve()
            repo, home = root / "repo", root / "codex-home"
            repo.mkdir()
            home.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            fixture = repo / "fixture.py"
            original = b"def calculate_fixture():\n    return 42\n"
            fixture.write_bytes(original)
            (repo / "caller.py").write_text("from fixture import calculate_fixture\n\n"
                                            "def invoke():\n    return calculate_fixture()\n", encoding="utf-8")
            macro_store = repo / ".graphflow" / "macros.json"
            macro_store.parent.mkdir()
            old_log_data = b"legacy first\nlegacy selected\nlegacy third"
            old_log_id = hashlib.sha256(old_log_data).hexdigest()[:12]
            old_log = macro_store.parent / "observations" / f"{old_log_id}.log"
            old_log.parent.mkdir()
            old_log.write_bytes(old_log_data)
            (repo / ".cursor").mkdir()
            (repo / ".cursor" / "mcp.json").write_text('{"marker":"private-client-config"}', encoding="utf-8")
            macro_store.write_text(json.dumps({"macros": [{"id": "fixture-bundle", "kind": "context-bundle",
                                                          "steps": ["fixture.py#calculate_fixture"]}]}), encoding="utf-8")
            journal = macro_store.with_name("session-journal.jsonl")
            journal_data = (b'{"kind":"pending-episode","createdAt":0,"turnId":"legacy-turn-11","task":"private-journal-text"}\n'
                            b'{"kind":"pending-episode","status":"private-journal-status"}\ninvalid\n')
            journal.write_bytes(journal_data)
            (repo / "fixture_test.py").write_text(
                "import unittest\nfrom fixture import calculate_fixture\n\n"
                "class FixtureTest(unittest.TestCase):\n"
                "    def test_changed(self):\n        self.assertEqual(calculate_fixture(), 43)\n",
                encoding="utf-8")
            subprocess.run(["git", "add", "fixture.py", "caller.py"], cwd=repo, check=True, capture_output=True)
            layer = graphflow_codex.preview(repo, root / "soul", disable_server="graphflow",
                                             include_hook=True, tasks=True, client_root=root, edit_queue=True)
            state = root / "state"
            engine_base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
            prefix = os.environ.get("STARWISH_GRAPHFLOW_PREFIX") or str(engine_base / "StarWish" / f"graphflow-{graphflow.VERSION}")
            shared = {
                "LOCALAPPDATA": str(state), "XDG_STATE_HOME": str(state),
                "STARWISH_GRAPHFLOW_PREFIX": prefix, "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1",
                "STARWISH_GRAPHFLOW_TEST_AGENT_LOGS": "1", "STARWISH_GRAPHFLOW_TEST_PROFILES": json.dumps({
                    "fixture": {"argv": [sys.executable, "-B", "-m", "unittest", "fixture_test", "-v"],
                                "paths": ["fixture.py"], "timeout": 15}}),
            }
            mcp_env = {**{key: value for key, value in shared.items() if key != "STARWISH_GRAPHFLOW_EDIT_QUEUE"},
                       "UV_OFFLINE": "1", "STARWISH_GRAPHFLOW_LEGACY_MACROS": "1",
                       "STARWISH_GRAPHFLOW_LEGACY_TRAJECTORIES": "1", "STARWISH_GRAPHFLOW_LEGACY_CODEMODS": "1",
                       "STARWISH_GRAPHFLOW_LEGACY_JOURNAL": "1",
                       "STARWISH_GRAPHFLOW_LEGACY_OBSERVATIONS": "1",
                       "STARWISH_GRAPHFLOW_LEGACY_TOPIC_RESUME": "1",
                       "STARWISH_GRAPHFLOW_LEGACY_TASK_IMPORT": "1",
                       "STARWISH_GRAPHFLOW_LEGACY_COMMAND_MAP": json.dumps({"fixture-verify": ["fixture"]})}
            uv = shutil.which("uv")
            if uv:
                cache = subprocess.run([uv, "cache", "dir"], capture_output=True, encoding="utf-8",
                                       check=True, timeout=10).stdout.strip()
                mcp_env["UV_CACHE_DIR"] = cache
            anchor = 'STARWISH_GRAPHFLOW_TASKS = "1"'
            layer = layer.replace(anchor, anchor + "\n" + "\n".join(
                f"{key} = {graphflow_codex._toml(value)}" for key, value in mcp_env.items()))
            layer = layer.replace("[mcp_servers.graphflow]\n", '[mcp_servers.graphflow]\ncommand = "node"\nargs = ["legacy.js"]\n')
            model = ('model = "starwish-fixture"\nmodel_provider = "starwish_fixture"\n'
                     '[analytics]\nenabled = false\n'
                     '[model_providers.starwish_fixture]\nname = "StarWish fixture"\n'
                     f'base_url = "{model_url}"\nwire_api = "responses"\nrequires_openai_auth = false\n'
                     'request_max_retries = 0\nstream_max_retries = 0\n')
            trust_project = f'\n[projects.{graphflow_codex._toml(str(repo))}]\ntrust_level = "trusted"\n'
            (home / "config.toml").write_text(model + layer + trust_project, encoding="utf-8")
            env = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("CODEX_", "OPENAI_", "STARWISH_"))}
            env["CODEX_HOME"] = str(home)
            env["NO_PROXY"] = "127.0.0.1,localhost"
            marker = "STARWISH-ISOLATED-PROBE"
            (root / "soul").mkdir()
            (root / "soul" / "core.json").write_text(
                json.dumps({"sections": {"identity": {"content": marker}}}), encoding="utf-8")
            with patch.dict(os.environ, env, clear=True):
                verified = graphflow_codex_probe.probe(codex, root, repo, "fixture.py#calculate_fixture", marker)
                installed = graphflow_codex_probe.probe(codex, root, repo, "fixture.py#calculate_fixture",
                                                       project_only=True, caller_file="caller.py", preview_line=2)
            self.assertFalse((state / "StarWish" / "project-edits").exists())
            self.assertEqual(verified["starwish"], "connected")
            self.assertEqual(installed["legacy"]["graphflow"], "disabled")
            self.assertIn("unapproved-test-rejected", installed["checks"])
            self.assertTrue({"task-backlog", "single-file-index-refresh", "edit-diff-without-receipt",
                             "impact-symbol", "impact-caller",
                             "change-plan-approval-gates",
                             "workflow-preview", "read-only-macro-replay"}
                            <= set(installed["checks"]))
            self.assertFalse(installed["memory_tools_called"] or installed["model_turn_started"])
            self.assertEqual(verified["legacy"], {"graphflow": "disabled", "user-graphflow": "absent"})
            process = subprocess.Popen([codex, "app-server", "--stdio"], cwd=repo, env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                       encoding="utf-8")
            messages = queue.Queue()

            def read_messages():
                for line in process.stdout:
                    messages.put(line)
                messages.put(None)

            drain = threading.Thread(target=read_messages, daemon=True)
            drain.start()
            request_id = 0
            notifications = []
            server_requests = []

            def send(payload):
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()

            def receive(until):
                try:
                    line = messages.get(timeout=max(.01, until - time.monotonic()))
                except queue.Empty:
                    return None
                if line is None:
                    return None
                message = json.loads(line)
                if "method" in message and "id" not in message:
                    notifications.append(message)
                elif "method" in message:
                    server_requests.append(message["method"])
                return message

            def rpc(method, params):
                nonlocal request_id
                request_id += 1
                send({"id": request_id, "method": method, "params": params})
                until = time.monotonic() + 25
                while time.monotonic() < until:
                    response = receive(until)
                    if response is None:
                        break
                    if response.get("id") == request_id:
                        self.assertNotIn("error", response, response)
                        return response["result"]
                stderr.seek(0)
                self.fail(f"Missing response for {method}: {stderr.read()[-3000:].decode('utf-8', 'replace')}")

            def hook_info(event="sessionStart"):
                listed = rpc("hooks/list", {"cwds": [str(repo)]})["data"]
                return next(hook for entry in listed for hook in entry["hooks"] if hook["eventName"] == event)

            def compact_thread(thread):
                start = len(notifications)
                rpc("thread/compact/start", {"threadId": thread})
                until = time.monotonic() + 25
                while True:
                    notes = [n for n in notifications[start:] if n["params"].get("threadId") == thread]
                    ended = next((n for n in notes if n["method"] == "turn/completed"), None)
                    if ended is not None:
                        self.assertEqual(ended["params"]["turn"]["status"], "completed", ended["params"]["turn"])
                        break
                    if time.monotonic() >= until or receive(until) is None:
                        self.fail("Timed out waiting for isolated Codex compaction.")
                self.assertTrue(any(n["method"] == "thread/compacted" or (
                    n["method"] == "item/completed" and n["params"]["item"]["type"] == "contextCompaction"
                ) for n in notes), "The native compaction did not complete.")
                while not model_requests.empty():
                    model_requests.get_nowait()
                return [n["params"]["run"] for n in notes if n["method"] == "hook/completed"
                        and n["params"]["run"]["eventName"] == "preCompact"]

            def probe_turn(thread, text="Local fixture only.", *, expect_compaction=False):
                first = len(notifications)
                rpc("turn/start", {"threadId": thread, "input": [
                    {"type": "text", "text": text, "text_elements": []}]})
                until = time.monotonic() + 25
                while True:
                    completed = next((note["params"]["turn"] for note in notifications[first:]
                                      if note["method"] == "turn/completed" and note["params"]["threadId"] == thread), None)
                    if completed is not None:
                        self.assertEqual(completed["status"], "completed", completed.get("error"))
                        break
                    if time.monotonic() >= until or receive(until) is None:
                        self.fail(f"Timed out waiting for the isolated turn. Server requests: {server_requests[-8:]}; "
                                  f"events: {[note['method'] for note in notifications[first:][-12:]]}; "
                                  f"pending model requests: {model_requests.qsize()}")
                path, body = model_requests.get(timeout=5)
                if expect_compaction:
                    notes = [note for note in notifications[first:] if note["params"].get("threadId") == thread]
                    self.assertTrue(any(n["method"] == "thread/compacted" or (
                        n["method"] == "item/completed" and n["params"]["item"]["type"] == "contextCompaction"
                    ) for n in notes), [(n["method"], n["params"].get("tokenUsage")) for n in notes])
                    while not model_requests.empty():
                        path, body = model_requests.get_nowait()
                self.assertTrue(path.endswith("/responses"), path)
                hooks = [note["params"]["run"] for note in notifications[first:]
                         if note["method"] == "hook/completed" and note["params"]["threadId"] == thread]
                return json.dumps(body, ensure_ascii=False), hooks

            try:
                rpc("initialize", {"clientInfo": {"name": "starwish-test", "version": "0.1"},
                                   "capabilities": {"experimentalApi": True}})
                send({"method": "initialized"})
                started = rpc("thread/start", {"cwd": str(repo), "ephemeral": True})
                thread_id = started["thread"]["id"]
                until = time.monotonic() + 12
                while True:
                    servers = rpc("mcpServerStatus/list", {"threadId": thread_id, "detail": "toolsAndAuthOnly"})["data"]
                    by_name = {row["name"]: row for row in servers}
                    status = by_name.get("starwish", {}).get("runtimeStatus")
                    if status not in (None, "starting") or time.monotonic() >= until:
                        break
                    time.sleep(.15)
                self.assertEqual(by_name["graphflow"]["runtimeStatus"], "disabled")
                self.assertFalse(by_name["graphflow"]["tools"])
                self.assertEqual(by_name["starwish"]["runtimeStatus"], "connected")
                self.assertIn("graphflow_context", by_name["starwish"]["tools"])
                self.assertIn("recall", by_name["starwish"]["tools"])
                self.assertFalse({"graphflow_edit_apply", "graphflow_edit_rollback", "graphflow_test_run"}
                                 & by_name["starwish"]["tools"].keys())

                def call(tool, **arguments):
                    response = rpc("mcpServer/tool/call", {"threadId": thread_id, "server": "starwish",
                                                           "tool": tool, "arguments": arguments})
                    self.assertFalse(response.get("isError"), response)
                    return response["content"][0]["text"]

                context = json.loads(call("graphflow_context", query="calculate_fixture"))
                self.assertEqual(context["anchors"][0]["read_target"], "fixture.py:1-2", context)
                indexed = json.loads(call("graphflow_index", rootDir=str(repo)))
                self.assertEqual(indexed["parsed_or_reused"], 2)
                exported = json.loads(call("graphflow_artifact", mode="export", rootDir=str(repo)))
                imported = json.loads(call("graphflow_artifact", mode="import", artifact=exported["artifact"], rootDir=str(repo)))
                self.assertTrue(imported["verified"])
                self.assertEqual(imported["file_count"], 2)
                graph = json.loads(call("graphflow_graph", target="fixture.py", rootDir=str(repo)))
                self.assertIn("calculate_fixture", [node.get("symbol") for node in graph["nodes"]])
                self.assertIn("defines", [edge["kind"] for edge in graph["edges"]])
                self.assertFalse(imported["workspace_written"])
                self.assertIn("return 42", json.loads(call("graphflow_read", target="fixture.py:1-2"))["text"])
                compat = call("gf_read", targets=["fixture.py#calculate_fixture", "fixture.py:1-2"], rootDir=str(repo))
                legacy = call("gf_read", resumeFromTurnId="legacy-turn-11", targets=["fixture.py#calculate_fixture"])
                self.assertIn("[Topic Resume: legacy-turn-11]", legacy)
                self.assertIn("return 42", legacy)
                self.assertNotIn("private-journal-text", legacy)
                self.assertIn("return 42", compat)
                dedup_args = {"targets": ["fixture.py:1-2"], "dedup": True, "sessionId": "codex-fixture-read-a"}
                self.assertIn("return 42", call("gf_read", **dedup_args))
                self.assertIn("读取结果未变化", call("gf_read", **dedup_args))
                self.assertIn("return 42", call("gf_read", **{**dedup_args, "sessionId": "codex-fixture-read-b"}))
                macro = json.loads(call("gf_macro", action="preview", id="fixture-bundle"))
                macro_result = json.loads(call("gf_macro", action="run", id="fixture-bundle",
                                               plan_sha256=macro["plan_sha256"]))
                self.assertIn("return 42", macro_result["steps"][0]["text"])
                self.assertFalse(macro_result["files_written"])
                self.assertTrue(call("recall_index"))
                planned = json.loads(call("graphflow_plan", task="Review calculate_fixture", target="fixture.py#calculate_fixture",
                                          mode="insight", intent="change", rootDir=str(repo)))
                self.assertFalse(planned["executed"])
                self.assertIn("apply", planned["plan"]["dag"]["nodes"])
                plan = planned["read_only_workflow"]
                replay = json.loads(call("graphflow_workflow_replay", **plan))
                self.assertFalse(replay["test_executed"])
                self.assertEqual(replay["state"], "completed")
                episode = json.loads(call("graphflow_run", task="Review calculate_fixture", target=plan["target"],
                                          requestId="codex-fixture-task"))
                self.assertFalse(episode["executed"])
                task = {"id": episode["episodeId"]}
                resumed = call("gf_read", topicId=task["id"])
                self.assertIn(f"Topic Resume: {task['id']}", resumed)
                self.assertIn("return 42", resumed)
                brief = call("gf_read", targets=["@brief"])
                self.assertIn("Cross-Agent Project Onboarding & Handoff Brief", brief)
                self.assertIn(task["id"], brief)
                self.assertEqual(brief, call("gf_read"))
                self.assertEqual(json.loads(call("gf_macro", action="task-list"))["items"][0]["id"], task["id"])
                diagnostic = json.loads(call("graphflow_diagnose", rootDir=str(repo)))
                backlog = diagnostic["legacy_journal"]
                node, slicer, _ = graphflow._engine(Path(prefix))
                upstream = json.loads(subprocess.run(
                    [node, "-e", "console.log(JSON.stringify(require(process.argv[1]).inspectFlywheel(process.argv[2])))",
                     str(slicer.with_name("flywheel.js")), str(repo)],
                    cwd=repo, capture_output=True, encoding="utf-8", check=True, timeout=15,
                ).stdout)
                for starwish_key, upstream_key in (("total_lines", "totalLines"), ("parse_errors", "parseErrors"),
                                                   ("pending", "pendingTotal"), ("over_ttl", "overTtl")):
                    self.assertEqual(backlog[starwish_key], upstream[upstream_key])
                self.assertNotIn("private-journal-", json.dumps(diagnostic))
                self.assertEqual(journal.read_bytes(), journal_data)
                self.assertEqual(diagnostic["engine"], "ready")
                self.assertEqual(diagnostic["index"]["status"], "cached")
                self.assertEqual(diagnostic["tasks"]["pending"], 1)
                self.assertNotIn(task["id"], json.dumps(diagnostic))
                saved = json.loads(call("gf_macro", action="task-run", id=task["id"]))
                self.assertEqual(saved["state"], "preview")
                replayed = json.loads(call("gf_macro", action="task-run", id=task["id"],
                                           plan_sha256=saved["plan"]["plan_sha256"]))
                self.assertEqual(replayed["state"], "completed")
                self.assertFalse(replayed["task_updated"] or replayed["test_executed"])
                companion = repo / "companion.py"
                companion.write_bytes(b"FLAG = 1\n")
                subprocess.run(["git", "add", "companion.py"], cwd=repo, check=True, capture_output=True)
                proposal = json.loads(call("gf_edit", file="fixture.py", rootDir=str(repo),
                                           edits=[{"oldCode": "    return 42", "newCode": "    return 43"},
                                                  {"file": "companion.py", "oldCode": "FLAG = 1", "newCode": "FLAG = 2"}]))
                self.assertEqual([part["path"] for part in proposal["files"]], ["companion.py", "fixture.py"])
                self.assertTrue(proposal["queued"])
                self.assertEqual(fixture.read_bytes(), original)
                approval = "isolated-codex-test-operator-key"
                request_env = {**shared, "STARWISH_GRAPHFLOW_TASKS": "1",
                               "STARWISH_GRAPHFLOW_WORKSPACE": str(repo), "STARWISH_GRAPHFLOW_OPERATOR_KEY": approval}
                with patch.dict(os.environ, request_env), patch.object(config, "SOUL_DIR", root / "soul"):
                    api = FastAPI()
                    api.include_router(router)
                    with TestClient(api, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as panel:
                        base = "/api/graphflow/"
                        auth = {"X-StarWish-Operator-Key": approval}
                        item, = panel.get(base + "edit/queue", headers=auth).json()
                        self.assertEqual(item["diff"], proposal["diff"])
                        receipt = {"preview_id": item["preview_id"]}
                        apply = {**receipt, "confirmed_sha256": item["result_sha256"]}
                        self.assertEqual(panel.post(base + "edit/apply", json=apply).status_code, 403)
                        applied = panel.post(base + "edit/apply", json=apply, headers=auth)
                        self.assertEqual(applied.status_code, 200, applied.text)
                        self.assertEqual(applied.json()["state"], "applied")
                        self.assertEqual(companion.read_bytes(), b"FLAG = 2\n")
                        refreshed = json.loads(call("graphflow_index", filePath="fixture.py"))
                        self.assertEqual(refreshed["index"]["updated"], 1)
                        self.assertIn("return 43", json.loads(call("graphflow_read", target="fixture.py:1-2"))["text"])
                        self.assertIn("return 43", call("gf_read", resumeFromTurnId=task["id"]))
                        profile, = panel.get(base + "test/plan", headers=auth).json()["profiles"]
                        data = json.loads(macro_store.read_text(encoding="utf-8"))
                        data["macros"].append({"id": "fixture-verify", "kind": "command-template", "steps": ["legacy {{arg1}}"]})
                        macro_store.write_text(json.dumps(data), encoding="utf-8")
                        verify = json.loads(call("gf_macro", action="preview", id="fixture-verify"))
                        handoff = json.loads(call("gf_macro", action="run", id="fixture-verify", plan_sha256=verify["plan_sha256"]))
                        self.assertEqual(handoff["state"], "awaiting_operator")
                        self.assertEqual(handoff["steps"], [{"profile": profile["profile"], "available": True}])
                        self.assertEqual(panel.get(base + "test/records", headers=auth).json(), [])
                        run = panel.post(base + "test/run", headers=auth, json={
                            "profile": profile["profile"], "approval_sha256": profile["approval_sha256"],
                            "task_id": task["id"],
                        })
                        self.assertEqual(run.status_code, 200, run.text)
                        outcome = run.json()
                        self.assertEqual((outcome["state"], outcome["exit_code"]), ("passed", 0))
                        self.assertEqual(outcome["task_verification"]["status"], "linked")
                        task_detail = json.loads(call("graphflow_task_detail", task_id=task["id"]))
                        self.assertEqual((task_detail["state"], task_detail["revision"]), ("pending", 0))
                        self.assertEqual(task_detail["test_verifications"][0]["run_id"], outcome["run_id"])
                        logs = json.loads(call("graphflow_test_log", run_id=outcome["run_id"]))
                        self.assertIn("test_changed", logs["text"])
                        self.assertIn("test_changed", call("gf_exec", handle=outcome["log_handle"], range=[1, 5]))
                        self.assertIn("test_changed", call("gf_exec", handle=outcome["observation_handle"], range=[1, 5]))
                        self.assertEqual(call("gf_exec", handle=f"gfo:{old_log_id}", range=[2, 2]),
                                         f"# Observation gfo:{old_log_id} (lines 2-2 of 3)\n   2: legacy selected")
                        self.assertEqual(old_log.read_bytes(), old_log_data)
                        doctor = call("gf_exec", cmd="doctor")
                        self.assertIn("PASS [graphflow-status]", doctor)
                        self.assertIn("cursor: mcp", doctor)
                        self.assertIn("effective registration not checked", doctor)
                        self.assertNotIn("private-client-config", doctor)
                        refused = rpc("mcpServer/tool/call", {"threadId": thread_id, "server": "starwish",
                                      "tool": "gf_exec", "arguments": {"cmd": "affected-tests", "run": True}})
                        self.assertTrue(refused["isError"])
                        done = json.loads(call("graphflow_report_outcome", episodeId=task["id"], revision=0, success=True,
                                               lessons=["calculate_fixture reviewed."],
                                               references=["code:fixture.py:1-2"]))
                        before_undo = json.loads(call("graphflow_context", query="calculate_fixture"))["outcomes"][0]
                        self.assertEqual(before_undo["task_id"], task["id"])
                        self.assertEqual(before_undo["evidence"][0]["source_state"], "matching_bytes")
                        self.assertEqual(before_undo["historical_test_verification"]["state"], "passed")
                        self.assertEqual(before_undo["historical_test_verification"]["run_id"], outcome["run_id"])
                        self.assertEqual(before_undo["historical_test_verification"]["plan_state"], "matching_plan")
                        brief = call("gf_read", targets=["@brief"])
                        self.assertIn("calculate_fixture reviewed.", brief)
                        self.assertIn("[passed]", brief)
                        self.assertIn("关联测试 fixture [passed]", brief)
                        self.assertNotIn("test_changed", brief)
                        undo = panel.post(base + "edit/rollback", headers=auth, json={
                            **receipt, "confirmed_sha256": item["base_sha256"],
                        })
                        self.assertEqual(undo.status_code, 200, undo.text)
                        self.assertEqual(undo.json()["state"], "rolled_back")
                        decl = b"export function collectEntry() { return 42; }\n"
                        caller = b"import { collectEntry } from './declaration.js';\nexport const value = collectEntry();\n"
                        (repo / "declaration.js").write_bytes(decl)
                        (repo / "caller.js").write_bytes(caller)
                        subprocess.run(["git", "add", "declaration.js", "caller.js"], cwd=repo, check=True, capture_output=True)
                        rename = json.loads(call("gf_edit", file="declaration.js", symbol="collectEntry",
                                                 renameTo="gatherEntry", rootDir=str(repo)))
                        self.assertEqual(rename["mode"], "rename")
                        self.assertEqual((repo / "caller.js").read_bytes(), caller)
                        rename_receipt = {"preview_id": rename["preview_id"]}
                        applied_rename = panel.post(base + "edit/apply", headers=auth, json={
                            **rename_receipt, "confirmed_sha256": rename["result_sha256"]})
                        self.assertEqual(applied_rename.status_code, 200, applied_rename.text)
                        self.assertEqual((repo / "caller.js").read_bytes(), caller.replace(b"collectEntry", b"gatherEntry"))
                        undo_rename = panel.post(base + "edit/rollback", headers=auth, json={
                            **rename_receipt, "confirmed_sha256": rename["base_sha256"]})
                        self.assertEqual(undo_rename.status_code, 200, undo_rename.text)
                        self.assertEqual(((repo / "declaration.js").read_bytes(), (repo / "caller.js").read_bytes()), (decl, caller))
                        data = json.loads(macro_store.read_text(encoding="utf-8"))
                        data["macros"].append({"id": "fixture-codemod", "kind": "edit-codemod",
                                               "steps": ["FLAG = 1", "FLAG = 9"], "scriptPath": "never-run.js"})
                        macro_store.write_text(json.dumps(data), encoding="utf-8")
                        codemod_args = {"id": "fixture-codemod", "args": ["companion.py"]}
                        preview_codemod = json.loads(call("gf_macro", action="preview", **codemod_args))
                        self.assertFalse(preview_codemod["queued"])
                        codemod = json.loads(call("gf_macro", action="run", **codemod_args,
                                                  plan_sha256=preview_codemod["plan_sha256"]))
                        self.assertTrue(codemod["queued"])
                        self.assertEqual(companion.read_bytes(), b"FLAG = 1\n")
                        for operation, key, expected in (("apply", "result_sha256", b"FLAG = 9\n"),
                                                         ("rollback", "base_sha256", b"FLAG = 1\n")):
                            response = panel.post(base + "edit/" + operation, headers=auth, json={
                                "preview_id": codemod["preview_id"], "confirmed_sha256": codemod[key]})
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertEqual(companion.read_bytes(), expected)
                self.assertEqual(fixture.read_bytes(), original)
                self.assertEqual(companion.read_bytes(), b"FLAG = 1\n")
                self.assertEqual(json.loads(call("graphflow_index"))["index"]["updated"], 2)
                self.assertEqual(json.loads(call("graphflow_edit_check", **receipt))["state"], "rolled_back")
                self.assertEqual(done["state"], "completed")
                skills = json.loads(call("graphflow_skill_insights", limit=1, rootDir=str(repo)))
                self.assertEqual(skills["patterns"][0]["example_task_ids"], [task["id"]])
                self.assertEqual(skills["patterns"][0]["passed_test_reports"], 1)
                self.assertFalse(skills["current_verification"])
                self.assertEqual(json.loads(call("graphflow_skill_guide"))["server"], "starwish")
                context = json.loads(call("graphflow_context", query="calculate_fixture"))
                self.assertEqual(context["outcomes"][0]["task_id"], task["id"])
                self.assertEqual(context["outcomes"][0]["evidence"][0]["source_state"], "changed")
                self.assertEqual(context["outcomes"][0]["historical_test_verification"],
                                 {**before_undo["historical_test_verification"], "plan_state": "changed_plan"})
                git_commit = ["git", "-c", "user.name=StarWish", "-c", "user.email=fixture@example.test",
                              "-c", "commit.gpgsign=false", "-c", "core.hooksPath=.git", "commit", "-qm"]
                subprocess.run([*git_commit, "fixture history"], cwd=repo, check=True, capture_output=True)
                (repo / "declaration.js").write_bytes(decl + b"// revision\n")
                (repo / "caller.js").write_bytes(caller + b"// revision\n")
                subprocess.run(["git", "add", "declaration.js", "caller.js"], cwd=repo, check=True, capture_output=True)
                subprocess.run([*git_commit, "shared revision"], cwd=repo, check=True, capture_output=True)
                history = json.loads(call("gf_macro", action="mine-git", rootDir=str(repo)))
                self.assertEqual(history["pairs"], [{"files": ["caller.js", "declaration.js"],
                                                      "co_commit_count": 2, "confidence": 1.0}])
                self.assertFalse(history["persisted"])
                (repo / "barrel.js").write_bytes(b"export { collectEntry } from './declaration.js';\n")
                (repo / "forwarded.js").write_bytes(b"import { collectEntry } from './barrel.js';\ncollectEntry();\n")
                (repo / "other.js").write_bytes(b"export function collectEntry() { return 56; }\n")
                subprocess.run(["git", "add", "--", "barrel.js", "forwarded.js", "other.js"],
                               cwd=repo, check=True, capture_output=True)
                with patch.dict(os.environ, request_env), patch.object(config, "SOUL_DIR", root / "soul"):
                    api = FastAPI()
                    api.include_router(router)
                    with TestClient(api, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as panel:
                        def check_forwarded(resolution, chain):
                            target = {"target": "declaration.js#collectEntry"}
                            result = json.loads(call("graphflow_impact", **target))
                            response = panel.post("/api/graphflow/impact", json=target)
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertEqual(result["impacts"], response.json()["impacts"])
                            record = next(row for row in result["impacts"][0]["importers"]
                                          if row["file"] == "forwarded.js")
                            self.assertEqual(record["resolution"], resolution)
                            self.assertEqual(record.get("reexport_chain"), chain)

                        check_forwarded("reexport_module", [{"file": "barrel.js", "line": 1}])
                        (repo / "barrel.js").write_bytes(b"export { collectEntry } from './other.js';\n")
                        check_forwarded("other_module", None)
                trace = repo / ".graphflow" / "trajectories.jsonl"
                trace_data = "\n".join(json.dumps({"kind": "read", "ok": True, "sessionId": "fixture-private-session",
                                                    "ts": str(n), "items": ["caller.js", "declaration.js"]})
                                         for n in (1, 2)).encode("utf-8")
                trace.write_bytes(trace_data)
                mined = call("gf_macro", action="mine")
                self.assertNotIn("fixture-private-session", mined)
                candidate, = json.loads(mined)["candidates"]
                trace_preview = json.loads(call("gf_macro", action="preview", id=candidate["id"]))
                trace_run = json.loads(call("gf_macro", action="run", id=candidate["id"],
                                           plan_sha256=trace_preview["plan_sha256"]))
                self.assertIn("collectEntry", trace_run["steps"][0]["text"])
                self.assertFalse(trace_run["files_written"])
                self.assertEqual(trace.read_bytes(), trace_data)
                self.assertEqual(json.loads(call("graphflow_legacy_task_import"))["legacy_ids"], ["legacy-turn-11"])
                migration = {"legacy_id": "legacy-turn-11", "target": "fixture.py#calculate_fixture"}
                leg_preview = json.loads(call("graphflow_legacy_task_import", action="preview", **migration))
                adopted = json.loads(call("graphflow_legacy_task_import", action="import", **migration,
                                          confirm_sha256=leg_preview["confirm_sha256"]))
                self.assertEqual(adopted["task"]["state"], "pending")
                self.assertNotIn("private-journal-text", json.dumps(leg_preview))
                self.assertEqual(journal.read_bytes(), journal_data)
                hook = hook_info()
                self.assertEqual(hook["trustStatus"], "untrusted")
                untrusted_prompt, _ = probe_turn(thread_id)
                hint = "use the starwish MCP graphflow_context tool first"
                self.assertNotIn(hint, untrusted_prompt)
                # Persist normal per-definition trust only in this disposable fixture.
                key = "hooks.state." + json.dumps(hook["key"]) + ".trusted_hash"
                rpc("config/batchWrite", {"edits": [{"keyPath": key, "value": hook["currentHash"],
                                                     "mergeStrategy": "upsert"}], "reloadUserConfig": True})
                self.assertEqual(hook_info()["trustStatus"], "trusted")
                trusted = rpc("thread/start", {"cwd": str(root), "ephemeral": True})["thread"]["id"]
                trusted_prompt, hooks = probe_turn(trusted)
                self.assertIn(hint, trusted_prompt)
                self.assertTrue(any(run["eventName"] == "sessionStart" and run["status"] == "completed"
                                    for run in hooks), hooks)
                self.assertNotIn(marker, trusted_prompt, "The current SessionStart does not push core memory.")

                def isolated_memory():
                    db = root / "soul" / "soul.db"
                    if not db.exists():
                        return None
                    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
                        return conn.execute("SELECT id, hash FROM stars ORDER BY id").fetchall()

                memory_before = isolated_memory()
                core_before = (root / "soul" / "core.json").read_bytes()
                self.assertFalse(compact_thread(trusted))
                self.assertEqual(isolated_memory(), memory_before)
                self.assertEqual((root / "soul" / "core.json").read_bytes(), core_before)
                rpc("config/batchWrite", {"edits": [{"keyPath": "hooks.SessionStart", "value": [
                    {"matcher": "startup|resume", "hooks": [{"type": "command", "timeout": 7,
                      "command": graphflow_codex._hook_command(repo, root)}]}], "mergeStrategy": "replace"}],
                    "reloadUserConfig": True})
                changed = hook_info()
                self.assertNotEqual(changed["currentHash"], hook["currentHash"])
                self.assertEqual(changed["trustStatus"], "modified")
                modified = rpc("thread/start", {"cwd": str(root), "ephemeral": True})["thread"]["id"]
                modified_prompt, _ = probe_turn(modified)
                self.assertNotIn(hint, modified_prompt)

                soul = root / "soul"
                core_path = soul / "core.json"
                core_data = json.loads(core_path.read_bytes())
                core_data["sections"]["preferences"] = {"content": "STARWISH-UNSELECTED-FIXTURE"}
                core_path.write_text(json.dumps(core_data), encoding="utf-8")
                with patch.object(config, "SOUL_DIR", soul):
                    graphflow_workspaces.discover(str(repo))
                    grant, = graphflow_workspaces.view()["workspaces"]
                    graphflow_workspaces.decide([grant["id"]], True)
                enabled_command = graphflow_codex._hook_command(repo, root, soul=soul, core_sections=("identity",))
                rpc("config/batchWrite", {"edits": [{"keyPath": "hooks.SessionStart", "value": [
                    {"matcher": "startup|resume", "hooks": [{"type": "command", "timeout": 7,
                      "command": enabled_command}]}], "mergeStrategy": "replace"}], "reloadUserConfig": True})
                enabled_hook = hook_info()
                self.assertEqual(enabled_hook["trustStatus"], "modified")
                new = rpc("thread/start", {"cwd": str(repo), "ephemeral": True})["thread"]["id"]
                untrusted_core_prompt, _ = probe_turn(new)
                self.assertNotIn(marker, untrusted_core_prompt)
                key = "hooks.state." + json.dumps(enabled_hook["key"]) + ".trusted_hash"
                rpc("config/batchWrite", {"edits": [{"keyPath": key, "value": enabled_hook["currentHash"],
                                                     "mergeStrategy": "upsert"}], "reloadUserConfig": True})
                before_core = core_path.read_bytes()
                new = rpc("thread/start", {"cwd": str(repo), "ephemeral": True})["thread"]["id"]
                injected, _ = probe_turn(new)
                self.assertIn(marker, injected)
                self.assertNotIn("STARWISH-UNSELECTED-FIXTURE", injected)
                self.assertEqual(core_path.read_bytes(), before_core)
                outside = rpc("thread/start", {"cwd": str(root), "ephemeral": True})["thread"]["id"]
                outside_prompt, _ = probe_turn(outside)
                self.assertIn(hint, outside_prompt)
                self.assertNotIn(marker, outside_prompt)
                with patch.object(config, "SOUL_DIR", soul):
                    graphflow_workspaces.decide([grant["id"]], False)
                new = rpc("thread/start", {"cwd": str(repo), "ephemeral": True})["thread"]["id"]
                revoked, _ = probe_turn(new)
                self.assertIn(hint, revoked)
                self.assertNotIn(marker, revoked)

                rpc("config/batchWrite", {"edits": [{"keyPath": "hooks.SessionStart", "value": [],
                    "mergeStrategy": "replace"}], "reloadUserConfig": True})
                layer_path = repo / ".codex" / "config.toml"
                layer_path.parent.mkdir(exist_ok=True)
                project_hook = graphflow_codex.preview(repo, root / "soul", include_hook=True)
                project_hook = "[[hooks.SessionStart]]" + project_hook.split("[[hooks.SessionStart]]", 1)[1]
                layer_path.write_text(project_hook, encoding="utf-8")
                hook = hook_info()
                self.assertEqual(Path(hook["sourcePath"]).resolve(), layer_path)
                key = "hooks.state." + json.dumps(hook["key"]) + ".trusted_hash"
                rpc("config/batchWrite", {"edits": [{"keyPath": key, "value": hook["currentHash"],
                                                     "mergeStrategy": "upsert"}], "reloadUserConfig": True})
                with patch.dict(os.environ, env, clear=True):
                    actual = graphflow_codex_probe.verify_hook(codex, repo, repo)
                    saved = graphflow_codex_probe.probe(codex, repo, repo, hooks_only=True, explain_trust=True)
                self.assertEqual(actual["status"], "verified", actual)
                self.assertTrue(actual["hint_injected"])
                self.assertFalse(actual["memory_tools_called"])
                self.assertEqual(saved["hook_trust_storage"]["runtime_trust"], "trusted")
                self.assertIn("matching", {layer["stored_definition_hash"]
                                           for layer in saved["hook_trust_storage"]["user_layers"]})
                self.assertFalse(saved["model_turn_started"])
                from app.watch.compact import QUEUE
                compact_profile = graphflow_codex.preview(repo, soul, include_precompact=True)
                layer_path.write_text(project_hook + "\n[[hooks.PreCompact]]"
                                      + compact_profile.split("[[hooks.PreCompact]]", 1)[1], encoding="utf-8")
                with patch.object(config, "SOUL_DIR", soul):
                    graphflow_workspaces.discover(str(repo))
                    graphflow_workspaces.decide([grant["id"]], True)
                compact_hook = hook_info("preCompact")
                self.assertEqual(Path(compact_hook["sourcePath"]).resolve(), layer_path)
                compact_key = "hooks.state." + json.dumps(compact_hook["key"]) + ".trusted_hash"
                rpc("config/batchWrite", {"edits": [
                    {"keyPath": compact_key, "value": compact_hook["currentHash"], "mergeStrategy": "upsert"}
                ], "reloadUserConfig": True})
                persisted = rpc("thread/start", {"cwd": str(repo), "ephemeral": False})["thread"]["id"]
                sample = "We check every Juniper Atlas translation with anonymous peer review."
                probe_turn(persisted, sample)
                rollout, = (home / "sessions").rglob(f"*{persisted}.jsonl")
                completed, = [row["payload"]["turn_id"] for row in map(json.loads, rollout.read_bytes().splitlines())
                              if row.get("type") == "event_msg" and row["payload"].get("type") == "task_complete"]
                for _ in range(2):
                    runs = compact_thread(persisted)
                    self.assertTrue(any(run["status"] == "completed" for run in runs), runs)
                self.assertTrue((soul / QUEUE).is_file(), "PreCompact did not stage an eligible completed turn.")
                with closing(sqlite3.connect(soul / QUEUE)) as db:
                    self.assertEqual(db.execute("SELECT session_id, turn_id FROM intents").fetchall(),
                                     [(persisted, completed)])
                self.assertNotIn(sample.encode(), (soul / QUEUE).read_bytes())
                probe_turn(persisted)
                with closing(sqlite3.connect(soul / QUEUE)) as db:
                    db.execute("BEGIN EXCLUSIVE")
                    self.assertTrue(compact_thread(persisted))
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM intents").fetchone(), (1,))
                with patch.object(config, "SOUL_DIR", soul):
                    graphflow_workspaces.decide([grant["id"]], False)
                self.assertTrue(compact_thread(persisted))
                with closing(sqlite3.connect(soul / QUEUE)) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM intents").fetchone(), (1,))
                self.assertEqual(isolated_memory(), memory_before)
                with patch.object(config, "SOUL_DIR", soul):
                    graphflow_workspaces.decide([grant["id"]], True)
                (soul / "config.json").write_text(json.dumps({
                    "llm": {"base_url": model_url, "api_key": "local-fixture", "model": "starwish-fixture"},
                    "watch": {"clients": {"codex": {"enabled": True, "path": str(home / "sessions")}}},
                }), encoding="utf-8")
                writeback_env = {**env, "STARWISH_SOUL_DIR": str(soul), "STARWISH_CODEX_COMPACT_WRITEBACK": "1",
                                 "STARWISH_WATCH_CODEX_ONLY_SESSION": persisted}

                def fresh_python(code, scope=persisted, *, writeback=True):
                    child_env = {**writeback_env}
                    if scope is None:
                        child_env.pop("STARWISH_WATCH_CODEX_ONLY_SESSION", None)
                    else:
                        child_env["STARWISH_WATCH_CODEX_ONLY_SESSION"] = scope
                    if not writeback:
                        child_env.pop("STARWISH_CODEX_COMPACT_WRITEBACK", None)
                    return subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code],
                                          cwd=graphflow_codex.ROOT, env=child_env,
                                          capture_output=True,
                                          check=True, timeout=30, encoding="utf-8").stdout

                worker_code = ("from app.watch.watcher import Watcher; w = Watcher(); "
                               "w.poll_once(); print(w.drain())")
                self.assertEqual(fresh_python(worker_code).splitlines()[-1], "1")
                path, request = model_requests.get(timeout=5)
                self.assertTrue(path.endswith("/chat/completions"))
                self.assertIn(sample, json.dumps(request))
                self.assertEqual(fresh_python(worker_code).splitlines()[-1], "0")
                recall_code = ("import json; from app.stars import recall; "
                               "print(json.dumps(recall('Juniper Atlas translations', bump=False)))")
                found, = json.loads(fresh_python(recall_code))["items"]
                self.assertIn(_COMPACT_SUMMARY.rstrip("."), found["summary"])
                self.assertFalse(json.loads(fresh_python(recall_code, scope=trusted))["items"])
                with closing(sqlite3.connect(soul / "soul.db")) as db:
                    self.assertEqual(db.execute("SELECT source, session, content FROM stars").fetchall(),
                                     [("watch:codex:precompact", persisted, _COMPACT_SUMMARY)])
                layer_path.write_text(project_hook, encoding="utf-8")

                # Use the normal file watcher, not a proxy or manual review call.
                fixture_cfg = json.loads((soul / "config.json").read_bytes())
                fixture_cfg["watch"].update(poll_sec=1, organize_after=False)
                for client_name in ("claude", "qoder", "workbuddy", "codebuddy", "opencode", "dsh"):
                    fixture_cfg["watch"]["clients"][client_name] = {"enabled": False}
                fixture_cfg["evolve"] = {"enabled": True, "nudge_interval": 3, "approval": "auto"}
                fixture_cfg["core"] = {"owner_token": "isolated-review-owner"}
                (soul / "config.json").write_text(json.dumps(fixture_cfg), encoding="utf-8")
                fresh_python(worker_code, scope=None, writeback=False)
                review_b = rpc("thread/start", {"cwd": str(repo), "ephemeral": False})["thread"]["id"]
                probe_turn(review_b, "SCOPED_SIBLING_FIXTURE prefers a different citation style.")
                review_a = rpc("thread/start", {"cwd": str(repo), "ephemeral": False})["thread"]["id"]
                probe_turn(review_a, "SCOPED_TARGET_FIRST: a manuscript lost a citation after renumbering.")
                probe_turn(review_a, "SCOPED_TARGET_SECOND: we audited the citation numbers before finalizing it.")
                review_waiter = """import sqlite3, time
from app import db
from app.evolve import skills
from app.watch.watcher import Watcher
w = Watcher()
w.start()
try:
    until = time.monotonic() + 18
    while time.monotonic() < until:
        p = [item for item in skills.list_pending() if item.get("source") == "review:codex"]
        try:
            with db.get_conn() as c:
                n = c.execute("SELECT COUNT(*) FROM codex_review_turns").fetchone()[0]
                processed = c.execute("SELECT SUM(reviewed) FROM codex_review_turns").fetchone()[0]
        except sqlite3.OperationalError:
            time.sleep(0.1)
            continue
        if (n, processed, len(p)) == EXPECTED:
            print(len(p), skills.find("check-source-numbering") is None)
            break
        time.sleep(0.1)
    else:
        print("review not ready", w.status)
finally:
    w.stop()
"""
                self.assertIn("0 True", fresh_python(review_waiter.replace("EXPECTED", "(3, 0, 0)"),
                                                     scope=None, writeback=False))
                while not model_requests.empty():
                    _, data = model_requests.get_nowait()
                    self.assertFalse(data.get("messages", [{}])[0].get("content", "").startswith("Review this Codex"))
                probe_turn(review_a, "SCOPED_TARGET_THIRD: the repeatable check is to verify each cited source number.")
                observed = fresh_python(review_waiter.replace("EXPECTED", "(4, 3, 2)"), scope=None, writeback=False)
                self.assertIn("2 True", observed, observed)
                requests = []
                while not model_requests.empty():
                    _, data = model_requests.get_nowait()
                    if data.get("messages") and data["messages"][0].get("content", "").startswith("Review this Codex"):
                        requests.append(data)
                scoped, = requests
                prompt = json.dumps(scoped)
                self.assertIn("SCOPED_TARGET_FIRST", prompt)
                self.assertIn("SCOPED_TARGET_SECOND", prompt)
                self.assertIn("SCOPED_TARGET_THIRD", prompt)
                self.assertNotIn("SCOPED_SIBLING_FIXTURE", prompt)
                with closing(sqlite3.connect(soul / "soul.db")) as db:
                    self.assertEqual(dict(db.execute("SELECT session_id,SUM(reviewed) FROM codex_review_turns "
                                                    "GROUP BY session_id")), {review_a: 3, review_b: 0})

                decisions = """from fastapi.testclient import TestClient
from app.server import app
c = TestClient(app)
h = {"X-StarWish-Owner": "isolated-review-owner"}
items = {x["op"]["name"]: x["id"] for x in c.get("/api/skills/pending", headers=h).json()}
print(c.post("/api/skills/pending/" + items["check-source-numbering"] + "/approve", headers=h).status_code)
print(c.post("/api/skills/pending/" + items["check-page-headers"] + "/reject", headers=h).status_code)
"""
                self.assertEqual(fresh_python(decisions, scope=None, writeback=False).splitlines()[-2:], ["200", "200"])
                # Allow only this read tool in the disposable Codex profile; the fixture has no interactive client.
                rpc("config/batchWrite", {"edits": [
                    {"keyPath": "mcp_servers.starwish.tools.skill_view.approval_mode", "value": "approve",
                     "mergeStrategy": "upsert"},
                ], "reloadUserConfig": True})
                independent = rpc("thread/start", {"cwd": str(repo), "ephemeral": True})["thread"]["id"]
                listing = rpc("mcpServer/tool/call", {"threadId": independent, "server": "starwish",
                                                     "tool": "skills_list", "arguments": {}})
                skill_list = json.dumps(listing)
                self.assertIn(_REVIEW_SKILL, skill_list)
                self.assertNotIn(_REJECT_SKILL, skill_list)
                first = len(notifications)
                applied_prompt, _ = probe_turn(independent, _SKILL_USAGE)
                applied_responses = notifications[first:]
                self.assertTrue(any(n["method"] == "item/completed" and
                                    "Citation 3 has no source entry." in json.dumps(n["params"]["item"])
                                    for n in applied_responses), applied_prompt[:1500])
                _, followup = model_requests.get(timeout=5)
                self.assertIn("Audit source numbering before finalizing", json.dumps(followup))
                self.assertTrue(model_requests.empty())

                auto_hooks = compact_profile.split("[[hooks.PreCompact]]", 1)[1].replace("manual|auto", "auto")
                layer_path.write_text(project_hook + "\n[[hooks.PreCompact]]" + auto_hooks, encoding="utf-8")
                auto_hook = hook_info("preCompact")
                self.assertEqual(auto_hook["trustStatus"], "modified")
                auto_key = "hooks.state." + json.dumps(auto_hook["key"]) + ".trusted_hash"
                rpc("config/batchWrite", {"edits": [
                    {"keyPath": auto_key, "value": auto_hook["currentHash"], "mergeStrategy": "upsert"},
                    {"keyPath": "model_auto_compact_token_limit", "value": 5000, "mergeStrategy": "upsert"},
                    {"keyPath": "model_context_window", "value": 30000, "mergeStrategy": "upsert"},
                ], "reloadUserConfig": True})
                auto_thread = rpc("thread/start", {"cwd": str(repo), "ephemeral": False})["thread"]["id"]
                _, first_runs = probe_turn(auto_thread, _AUTO_TRIGGER_SAMPLE)
                self.assertFalse(any(run["eventName"] == "preCompact" for run in first_runs))
                auto_rollout, = (home / "sessions").rglob(f"*{auto_thread}.jsonl")
                previous_turn, = [row["payload"]["turn_id"] for row in map(json.loads, auto_rollout.read_bytes().splitlines())
                                  if row.get("type") == "event_msg" and row["payload"].get("type") == "task_complete"]
                _, auto_runs = probe_turn(auto_thread, "Now compare the citation summary.", expect_compaction=True)
                self.assertTrue(any(run["eventName"] == "preCompact" and run["status"] == "completed"
                                    for run in auto_runs), auto_runs)
                with closing(sqlite3.connect(soul / QUEUE)) as db:
                    self.assertEqual(db.execute("SELECT turn_id FROM intents WHERE session_id=?", (auto_thread,)).fetchall(),
                                     [(previous_turn,)])
                layer_path.write_text(project_hook, encoding="utf-8")
                rpc("config/batchWrite", {"edits": [{"keyPath": "hooks.SessionStart",
                    "value": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo unrelated"}]}],
                    "mergeStrategy": "replace"}], "reloadUserConfig": True})
                user_config = (home / "config.toml").read_bytes()
                project_config = layer_path.read_bytes()
                with patch.dict(os.environ, env, clear=True):
                    blocked = graphflow_codex_probe.verify_hook(codex, repo, repo)
                    isolated = graphflow_codex_probe.verify_hook(codex, repo, repo, isolate_other_hooks=True)
                self.assertEqual(blocked["status"], "other_hooks_enabled", blocked)
                self.assertFalse(blocked["model_turn_started"])
                self.assertEqual(isolated["status"], "verified", isolated)
                self.assertEqual(isolated["other_hook_isolation"], {"requested": 1, "confirmed_disabled": 1})
                self.assertEqual((home / "config.toml").read_bytes(), user_config)
                self.assertEqual(layer_path.read_bytes(), project_config)
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                drain.join(timeout=2)
                process.stdout.close()
