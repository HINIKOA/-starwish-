"""GraphFlow adapter, workspace isolation and local HTTP contract."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, graphflow
from app.graphflow_routes import router


class GraphFlowTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(graphflow._clear_snapshot)

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_dependency_graph_is_bounded_scoped_and_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "lib.ts").write_text('export function total(input = "BODY_PRIVATE") { return input; }\n', encoding="utf-8")
            (root / "entry.ts").write_text("import { total } from './lib';\nexport function main() { return total(); }\n",
                                           encoding="utf-8")
            (root / "README.md").write_text("# DOC_PRIVATE\n\nUse `total` from `lib.ts` to compute the result.\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "lib.ts", "entry.ts", "README.md"], cwd=root, check=True)
            commit = ["git", "-c", "user.name=StarWish", "-c", "user.email=fixture@example.test",
                      "-c", "commit.gpgsign=false", "-c", "core.hooksPath=.git", "commit", "-qm"]
            subprocess.run([*commit, "PRIVATE_COMMIT_FIRST"], cwd=root, check=True, capture_output=True)
            for name in ("lib.ts", "entry.ts"):
                path = root / name
                path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "lib.ts", "entry.ts"], cwd=root, check=True)
            subprocess.run([*commit, "PRIVATE_COMMIT_SECOND"], cwd=root, check=True, capture_output=True)
            (root / "untracked.ts").write_text("export function untracked() {}\n", encoding="utf-8")
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(root), "STARWISH_GRAPHFLOW_ASSISTANT": "1"}):
                if not graphflow.status()["available"]:
                    if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                        self.fail(graphflow.status()["reason"])
                    self.skipTest("GraphFlow engine not installed")
                graph = graphflow.dependency_graph("lib.ts")
                self.assertEqual({row["file"] for row in graph["nodes"]}, {"entry.ts", "lib.ts", "README.md"})
                self.assertTrue(any(node.get("symbol") == "total" and node["line"] == 1 for node in graph["nodes"]))
                pairs = {(edge["kind"], graph["nodes"][edge["source"]]["file"], graph["nodes"][edge["target"]]["file"])
                         for edge in graph["edges"]}
                self.assertIn(("imports", "entry.ts", "lib.ts"), pairs)
                self.assertIn(("calls", "entry.ts", "lib.ts"), pairs)
                self.assertIn(("defines", "lib.ts", "lib.ts"), pairs)
                self.assertIn(("doc-links-code", "README.md", "lib.ts"), pairs)
                self.assertIn(("co-edit", "entry.ts", "lib.ts"), pairs)
                self.assertEqual([edge["co_commit_count"] for edge in graph["edges"] if edge["kind"] == "co-edit"], [2])
                self.assertEqual(graphflow.dependency_graph("README.md")["edges"][0]["kind"], "doc-links-code")
                for marker in ("BODY_PRIVATE", "DOC_PRIVATE", "PRIVATE_COMMIT", "compute the result", "untracked"):
                    self.assertNotIn(marker, json.dumps(graph))
                small = graphflow.dependency_graph("lib.ts", 1, 0)
                self.assertTrue(small["truncated"])
                self.assertEqual((len(small["nodes"]), len(small["edges"])), (1, 0))
                self.assertEqual(agent.run_tool("graphflow_graph", {"target": "lib.ts"})["nodes"], graph["nodes"])
                for kwargs, status in (({"target": "untracked.ts"}, 404), ({"target": "../other.ts"}, 400),
                                       ({"node_limit": True}, 400), ({"root_dir": str(root.parent)}, 403)):
                    with self.assertRaises(graphflow.GraphFlowError) as exc:
                        graphflow.dependency_graph(**kwargs)
                    self.assertEqual(exc.exception.status_code, status)
                app = FastAPI()
                app.include_router(router)
                with TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as client:
                    url = "/api/graphflow/graph"
                    body = {"target": "lib.ts"}
                    self.assertEqual(client.post(url, json=body).json()["edges"], graph["edges"])
                    self.assertEqual(client.post(url, json=body, headers={"Origin": "http://other.test"}).status_code, 403)
                (root / "entry.ts").unlink()
                updated = graphflow.dependency_graph("lib.ts")
                self.assertFalse(any(node["file"] == "entry.ts" for node in updated["nodes"]))
                self.assertNotIn("co-edit", {edge["kind"] for edge in updated["edges"]})
                with patch("app.graphflow_history.mine", side_effect=graphflow.GraphFlowError("fixture timeout", 504)):
                    degraded = graphflow.dependency_graph("lib.ts")
                self.assertEqual(degraded["history"]["status"], "unavailable_504")
                self.assertIn("defines", {edge["kind"] for edge in degraded["edges"]})
                self.assertFalse((root / ".graphflow").exists())

    def test_requires_explicit_workspace_and_assistant_opt_in(self):
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": "", "STARWISH_GRAPHFLOW_ASSISTANT": ""}):
            self.assertFalse(graphflow.status()["available"])
            self.assertNotIn("graphflow_read", {tool["function"]["name"] for tool in agent.tools()})
            self.assertIn("error", agent.run_tool("graphflow_read", {"query": "test"}))

    def test_rejects_ambiguous_and_outside_targets(self):
        for target in ("../secret.py", "C:/secret.py", "/etc/secret.py", ".private/test.py", "soul/test.py",
                       "test.py#thing#more", "test.py:stream"):
            with self.subTest(target=target), self.assertRaises(graphflow.GraphFlowError) as error:
                graphflow.read(target=target)
            self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(graphflow.GraphFlowError):
            graphflow.read(query="name", target="file.py")

    def test_batch_rejects_unsupported_options_before_opening_workspace(self):
        for args in ({"targets": ["safe.py", "../secret.py"]}, {"q": "example", "dedup": True},
                     {"q": "example", "topicId": "old-turn"}, {"q": "example", "topK": True},
                     {"q": "example", "topK": 13}, {"targets": ["safe.py"] * 13}, {"targets": "safe.py"}):
            with self.subTest(args=args), patch.object(graphflow, "_workspace") as workspace:
                with self.assertRaises(graphflow.GraphFlowError):
                    graphflow.read_batch(**args)
                workspace.assert_not_called()
        with patch.object(graphflow, "assistant_enabled", return_value=False):
            self.assertNotIn("gf_read", {tool["function"]["name"] for tool in agent.tools()})
            self.assertIn("error", agent.run_tool("gf_read", {"q": "test"}))

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_batch_real_slicing_assistant_and_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "repo").resolve()
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.py").write_text("def locate_a():\n    return 31415\n", encoding="utf-8")
            (root / "b.py").write_text("def locate_b():\n    return 92653\n", encoding="utf-8")
            (root / "private.py").write_text("PRIVATE_MARKER", encoding="utf-8")
            subprocess.run(["git", "add", "a.py", "b.py"], cwd=root, check=True)
            env = {"STARWISH_GRAPHFLOW_WORKSPACE": str(root), "STARWISH_GRAPHFLOW_ASSISTANT": "1",
                   "STARWISH_SOUL_DIR": str(root.parent / "soul"), "STARWISH_MCP_FALLBACK": "1"}
            targets = ["a.py#locate_a", "b.py:1-2", "a.py#locate_a"]
            with patch.dict(os.environ, env):
                if not graphflow.status()["available"]:
                    if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                        self.fail(graphflow.status()["reason"])
                    self.skipTest("GraphFlow engine not installed")
                with patch.object(graphflow, "_sync_snapshot", wraps=graphflow._sync_snapshot) as sync:
                    result = agent.run_tool("gf_read", {"targets": targets})
                    sync.assert_called_once()
                self.assertIn("gf_read", {tool["function"]["name"] for tool in agent.tools()})
                self.assertEqual(result["text"].count("return 31415"), 1)
                self.assertIn("return 92653", result["text"])
                self.assertIn("locate_a", graphflow.read_batch(q="locate_a", topK=1, skeleton=True)["text"])
                with self.assertRaises(graphflow.GraphFlowError) as outside:
                    graphflow.read_batch(targets, rootDir=str(root.parent))
                self.assertEqual(outside.exception.status_code, 403)
                with self.assertRaises(graphflow.GraphFlowError) as untracked:
                    graphflow.read_batch([*targets, "private.py"])
                self.assertEqual(untracked.exception.status_code, 404)

                messages = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                                "name": "gf_read", "arguments": {"targets": targets, "rootDir": str(root)}}},
                            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                                "name": "gf_edit", "arguments": {"file": "a.py", "startLine": 2, "endLine": 2,
                                                                "oldCode": "    return 31415", "newCode": "    return 1",
                                                                "preview_only": True}}}]
                server = Path(__file__).resolve().parents[1] / "mcp_server.py"
                proc = subprocess.run([sys.executable, str(server)], input="\n".join(map(json.dumps, messages)) + "\n",
                                      encoding="utf-8", capture_output=True, check=True, timeout=35)
                listed, called, edited = [json.loads(line)["result"] for line in proc.stdout.splitlines()]
                self.assertIn("gf_read", {tool["name"] for tool in listed["tools"]})
                self.assertFalse(called["isError"])
                self.assertEqual(called["content"][0]["text"], result["text"])
                self.assertFalse(edited["isError"])
                preview_only = json.loads(edited["content"][0]["text"])
                self.assertEqual(preview_only["state"], "preview_only")
                self.assertNotIn("preview_id", preview_only)

                args = {"targets": ["a.py:1-2"], "dedup": True, "sessionId": "fixture-session-a"}
                with patch.object(graphflow, "_read_dedup", OrderedDict()), \
                     patch.object(graphflow.time, "monotonic", return_value=100) as clock:
                    first = agent.run_tool("gf_read", args)
                    self.assertIn("return 31415", first["text"])
                    repeat = agent.run_tool("gf_read", args)
                    self.assertTrue(repeat["deduplicated"])
                    self.assertNotIn("return 31415", repeat["text"])
                    self.assertNotIn("_snapshot_digest", repeat)
                    self.assertEqual(first["text"], agent.run_tool("gf_read", {**args, "dedup": False})["text"])
                    self.assertEqual(first["text"], agent.run_tool("gf_read", {
                        **args, "sessionId": "fixture-session-b"})["text"])
                    (root / "a.py").write_text("def locate_a():\n    return 31415\n\n# change outside slice\n",
                                               encoding="utf-8")
                    self.assertNotIn("deduplicated", agent.run_tool("gf_read", args))
                    self.assertTrue(agent.run_tool("gf_read", args)["deduplicated"])
                    clock.return_value += graphflow._READ_DEDUP_TTL
                    self.assertNotIn("deduplicated", agent.run_tool("gf_read", args))
                    (root / "a.py").unlink()
                    self.assertIn("error", agent.run_tool("gf_read", args))

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_explicit_tail_targets_use_budget_without_changing_keyword_scope(self):
        import mcp_server

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            sources = {
                "a.ts": "export function caller() { return 0; }\n",
                "b.ts": "export function baseline() { return 1; }\n",
                "y.ts": "export function tailOther() { return 73; }\n",
                "z.ts": "export function tailTarget() { return 31901; }\n",
                "soul/local.ts": "export function excluded() { return 777; }\n",
                "untracked.ts": "export function untracked() { return 999; }\n",
            }
            for name, content in sources.items():
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(content, encoding="utf-8")
            subprocess.run(["git", "add", "--", *[name for name in sources if name != "untracked.ts"]],
                           cwd=root, check=True)
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(root)}), patch.object(graphflow, "MAX_FILES", 2):
                if not graphflow.status()["available"]:
                    if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                        self.fail(graphflow.status()["reason"])
                    self.skipTest("GraphFlow engine not installed")
                app = FastAPI()
                app.include_router(router)
                with TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000)) as client:
                    read = client.post("/api/graphflow/read", json={"target": "z.ts:1-1"}).json()
                    self.assertIn("31901", read["text"])
                    self.assertTrue(read["index"]["partial"])
                    self.assertEqual(read["index"]["files"], 2)
                    detail = client.post("/api/graphflow/impact", json={"target": "z.ts#tailTarget"}).json()
                    self.assertTrue(detail["impacts"])
                    self.assertTrue(detail["index"]["partial"])
                    for name, status in (("untracked.ts", 404), ("soul/local.ts", 400)):
                        with self.subTest(name=name):
                            response = client.post("/api/graphflow/read", json={"target": name})
                            self.assertEqual(response.status_code, status)
                batch = mcp_server.gf_read(["y.ts#tailOther", "z.ts#tailTarget"])
                self.assertIn("31901", batch)
                self.assertIn("return 73", batch)
                self.assertEqual(graphflow.reindex(file_path="z.ts")["unparsed"], 0)
                graph = graphflow.dependency_graph("z.ts")
                self.assertTrue(any(node["file"] == "z.ts" for node in graph["nodes"]))
                base = graphflow.read(query="tailTarget", anchors_only=True)
                self.assertTrue(base["index"]["partial"])
                self.assertEqual(base["matches"], [])
                self.assertEqual(set(graphflow._active_snapshot.files), {"a.ts", "b.ts"})

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_explicit_files_never_bypass_snapshot_byte_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            repo, dest = root / "repo", root / "snapshot"
            repo.mkdir()
            dest.mkdir()
            git = shutil.which("git")
            subprocess.run([git, "init", "-q"], cwd=repo, check=True)
            for name, size in {"a.py": 7, "b.py": 3, "y.py": 11, "z.py": 4}.items():
                (repo / name).write_bytes(b"x" * size)
            subprocess.run([git, "add", "--", "*.py"], cwd=repo, check=True)
            with patch.object(graphflow, "MAX_BYTES", 10), patch.object(graphflow, "MAX_FILE_BYTES", 10):
                partial, files, stats = graphflow._refresh_snapshot(repo, dest, git, {}, ("y.py", "z.py", "b.py"))
            self.assertTrue(partial)
            self.assertEqual(stats["files"], 2)
            self.assertEqual(set(files), {"b.py", "z.py"})

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_snapshot_evicts_tracked_file_that_becomes_a_hardlink(self):
        from app import config, graphflow_edits, graphflow_tests

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            repo, dest, soul = root / "repo", root / "snapshot", root / "soul"
            for folder in (repo, dest, soul):
                folder.mkdir()
            git = shutil.which("git")
            subprocess.run([git, "init", "-q"], cwd=repo, check=True, capture_output=True)
            tracked = repo / "module.py"
            tracked.write_bytes(b"def public_fixture():\n    return 42\n")
            subprocess.run([git, "add", "module.py"], cwd=repo, check=True, capture_output=True)
            outside = soul / "module.py"
            outside.write_bytes(b"def fixture_outside():\n    return 'synthetic private placeholder'\n")
            with patch.object(config, "SOUL_DIR", soul):
                _, before, _ = graphflow._refresh_snapshot(repo, dest, git, {})
                self.assertEqual(set(before), {"module.py"})
                tracked.unlink()
                try:
                    os.link(outside, tracked)
                except (NotImplementedError, OSError):
                    self.skipTest("filesystem does not support hardlinks")
                _, after, stats = graphflow._refresh_snapshot(repo, dest, git, before, ("module.py",))
                self.assertEqual(after, {})
                self.assertEqual(stats["removed"], 1)
                self.assertFalse((dest / "module.py").exists())
                with self.assertRaises(graphflow.GraphFlowError) as edit:
                    graphflow_edits._read_tracked(repo, "module.py")
                self.assertEqual(edit.exception.status_code, 404)
                with self.assertRaises(graphflow.GraphFlowError) as approval:
                    graphflow_tests._approval_inputs(repo, ["module.py"])
                self.assertEqual(approval.exception.status_code, 409)
                self.assertIn(b"synthetic private placeholder", outside.read_bytes())

    def test_verified_source_does_not_yield_a_replaced_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "source.py"
            replacement = root / "replacement.py"
            source.write_bytes(b"original fixture")
            replacement.write_bytes(b"replacement fixture")
            soul = root / "soul"
            checked = graphflow._source_metadata(source, soul)
            replacement.replace(source)
            with self.assertRaises(graphflow.GraphFlowError) as changed:
                with graphflow._open_source(source, soul, checked):
                    self.fail("changed file was made available to the reader")
            self.assertEqual(changed.exception.status_code, 409)

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_snapshot_and_real_slicing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            sources = {
                "sample.py": "def locate_example():\n    return 'visible_marker'\n",
                "pending.py": "untracked_marker\n",
                ".env": "hidden_marker\n",
                "soul/data.md": "private_memory_marker\n",
            }
            for name, text in sources.items():
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(text, encoding="utf-8")
            subprocess.run(["git", "add", "--", "sample.py", ".env", "soul/data.md"], cwd=root, check=True)
            dest = Path(temp) / "snapshot"
            dest.mkdir()
            partial, files, stats = graphflow._refresh_snapshot(root, dest, shutil.which("git"), {})
            self.assertFalse(partial)
            self.assertEqual(set(files), {"sample.py"})
            self.assertEqual(stats["added"], 1)
            self.assertEqual((dest / "sample.py").read_text(encoding="utf-8"), sources["sample.py"])
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(root)}):
                if graphflow.status()["available"]:
                    result = graphflow.read(target="sample.py#locate_example")
                    self.assertIn("sample.py:1-2", result["text"])
                    self.assertIn("visible_marker", result["text"])
                    self.assertIn("No symbols matched", graphflow.read(query="private_memory_marker")["text"])
                    with self.assertRaises(graphflow.GraphFlowError) as error:
                        graphflow.read(target="pending.py")
                    self.assertEqual(error.exception.status_code, 404)
                    self.assertFalse((root / ".graphflow").exists())
                elif os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                    self.fail(graphflow.status()["reason"])

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_multilingual_snapshot_matches_pinned_engine(self):
        examples = {
            "calc.ts": ("tsCalculation", "export function tsCalculation(): number { return 101; }\n"
                        "export function tsCaller() { return tsCalculation(); }\n"),
            "calc.py": ("python_calculation", "def python_calculation():\n    return 102\n\n"
                        "def python_caller():\n    return python_calculation()\n"),
            "calc.go": ("GoCalculation", "package calc\n\nfunc GoCalculation() int { return 103 }\n"
                        "func GoCaller() int { return GoCalculation() }\n"),
            "calc.rs": ("rust_calculation", "pub fn rust_calculation() -> i32 { 104 }\n"
                        "pub fn rust_caller() -> i32 { rust_calculation() }\n"),
            "calc.hxx": ("cppCalculation", "inline int cppCalculation() { return 105; }\n"),
            "caller.cxx": ("cppCaller", '#include "calc.hxx"\nint cppCaller() { return cppCalculation(); }\n'),
            "Calc.cs": ("CsCalculation", "public class Calc {\n"
                        "    public static int CsCalculation() { return 106; }\n"
                        "    public static int CsCaller() { return CsCalculation(); }\n}\n"),
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            repo, direct = root / "repo", root / "direct"
            repo.mkdir()
            direct.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            for file, (_, source) in examples.items():
                for folder in (repo, direct):
                    (folder / file).write_text(source, encoding="utf-8")
            subprocess.run(["git", "add", "--", *examples], cwd=repo, check=True)
            (repo / "private.cxx").write_text("int privateUntrackedSymbol() { return 777; }", encoding="utf-8")
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(repo)}):
                if not graphflow.status()["available"]:
                    if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                        self.fail(graphflow.status()["reason"])
                    self.skipTest("GraphFlow engine not installed")
                node, entry, _ = graphflow._engine()
                reference = SimpleNamespace(root=direct)
                targets = [f"{file}#{symbol}" for file, (symbol, _) in examples.items()]
                for file, (symbol, source) in examples.items():
                    with self.subTest(file=file):
                        target = f"{file}#{symbol}"
                        expected = graphflow._call_bridge(reference, node, entry, query=None, targets=[target],
                                                          topK=6, includeCallers=True, includeSkeleton=False)
                        actual = graphflow.read(target=target)
                        self.assertIn(f"{file}:", actual["text"])
                        declaration = next(line.strip() for line in source.splitlines() if symbol in line)
                        self.assertIn(declaration, actual["text"])
                        self.assertEqual(actual["text"], expected)
                        expected_impact = graphflow._call_bridge(reference, node, entry,
                                                                 impact={"file": file, "symbol": symbol})
                        self.assertTrue(expected_impact["impacts"], file)
                        self.assertEqual(graphflow.impact(target)["impacts"], expected_impact["impacts"])
                self.assertEqual(graphflow.read_batch(targets)["index"]["files"], len(examples))
                self.assertIn('#include "calc.hxx"', graphflow.read(target="caller.cxx:1-1")["text"])
                with self.assertRaises(graphflow.GraphFlowError) as private:
                    graphflow.read(target="private.cxx")
                self.assertEqual(private.exception.status_code, 404)
                self.assertFalse((repo / ".graphflow").exists())

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_incremental_refresh_and_impact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            target = root / "target.js"
            target.write_text("export function calculate() { return 101; }\n", encoding="utf-8")
            subprocess.run(["git", "add", "target.js"], cwd=root, check=True)
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(root)}):
                if not graphflow.status()["available"]:
                    if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                        self.fail(graphflow.status()["reason"])
                    self.skipTest("GraphFlow engine not installed")
                args = {"target": "target.js#calculate"}
                cold = graphflow.read(**args)
                self.assertEqual(cold["index"]["added"], 1)
                self.assertIn("return 101", cold["text"])
                active = graphflow._active_snapshot
                shard, = (active.root / ".graphflow" / "ast-cache").glob("*.json")
                shard_time = shard.stat().st_mtime_ns
                self.assertEqual(graphflow.read(**args)["index"]["updated"], 0)
                self.assertEqual(shard.stat().st_mtime_ns, shard_time)
                time_before = target.stat()
                target.write_text("export function calculate() { return 202; }\n", encoding="utf-8")
                os.utime(target, ns=(time_before.st_atime_ns, time_before.st_mtime_ns))
                result = graphflow.read(**args)
                self.assertEqual(result["index"]["updated"], 1)
                self.assertIn("return 202", result["text"])
                self.assertNotIn("return 101", result["text"])
                self.assertIn("return 202", graphflow.read(target="target.js:1-1")["text"])
                with self.assertRaises(graphflow.GraphFlowError):
                    graphflow.read(target="target.js:1-201")
                caller = root / "caller.js"
                caller.write_text("import { calculate } from './target.js';\nexport const total = calculate();\n",
                                  encoding="utf-8")
                self.assertEqual(graphflow.impact(**args)["impacts"][0]["total_call_sites"], 0)
                subprocess.run(["git", "add", "caller.js"], cwd=root, check=True)
                impact = graphflow.impact(**args)
                self.assertEqual(impact["index"]["added"], 1)
                self.assertIn(("caller.js", 2), {(site["file"], site["line"])
                                                for site in impact["impacts"][0]["call_sites"]})
                self.assertIn({"file": "caller.js", "line": 1, "resolved_file": "target.js",
                               "resolution": "direct_module"}, impact["impacts"][0]["importers"])
                caller.unlink()
                after = graphflow.impact(**args)
                self.assertEqual(after["index"]["removed"], 1)
                self.assertEqual(after["impacts"][0]["total_call_sites"], 0)
                self.assertFalse((active.root / "caller.js").exists())
                self.assertEqual(graphflow.reindex(file_path="target.js")["parsed_or_reused"], 1)
                self.assertEqual(graphflow.reindex()["parsed_or_reused"], 0)
                with self.assertRaises(graphflow.GraphFlowError) as outside:
                    graphflow.reindex(root_dir=str(root.parent))
                self.assertEqual(outside.exception.status_code, 403)
                rebuilt = graphflow.reindex(mode="full", root_dir=str(root))
                self.assertEqual(rebuilt["index"]["added"], 1)
                self.assertEqual(rebuilt["parsed_or_reused"], 1)
                self.assertNotEqual(active.root, graphflow._active_snapshot.root)
                target.unlink()
                with self.assertRaises(graphflow.GraphFlowError) as error:
                    graphflow.impact(**args)
                self.assertEqual(error.exception.status_code, 404)
                self.assertFalse((root / ".graphflow").exists())

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_cross_module_impact_distinguishes_import_destinations(self):
        sources = {
            "target.ts": "export function calculate() { return 1; }\n",
            "other.ts": "export function calculate() { return 2; }\n",
            "barrel.ts": "export { calculate } from './target';\n",
            "direct.ts": "import { calculate } from './target';\nexport const value = calculate();\n",
            "wrong.ts": "import { calculate } from './other';\nexport const value = calculate();\n",
            "forwarded.ts": "import { calculate } from './barrel';\nexport const value = calculate();\n",
            "alias.ts": "export { calculate as renamed } from './target';\n",
            "facade.ts": "import { renamed as local } from './alias';\nexport { local as calculate };\n",
            "multi.ts": "import { calculate } from './facade';\nexport const value = calculate();\n",
            "wildcard.ts": "export * from './target';\n",
            "wildcard-user.ts": "import { calculate } from './wildcard';\ncalculate();\n",
            "default-user.ts": "import calculate from './barrel';\ncalculate();\n",
            "cycle1.ts": "export { calculate } from './cycle2';\n",
            "cycle2.ts": "export { calculate } from './cycle1';\n",
            "cyclic-user.ts": "import { calculate } from './cycle1';\ncalculate();\n",
            "external.ts": "import { calculate } from 'some-package';\nexport const value = calculate();\n",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            for name, source in sources.items():
                (root / name).write_text(source, encoding="utf-8")
            subprocess.run(["git", "add", "--", *sources], cwd=root, check=True)
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(root)}):
                if not graphflow.status()["available"]:
                    if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                        self.fail(graphflow.status()["reason"])
                    self.skipTest("GraphFlow engine not installed")
                def destinations():
                    impact = graphflow.impact("target.ts#calculate")["impacts"][0]
                    self.assertGreaterEqual(impact["total_importers"], len(impact["importers"]))
                    return {row["file"]: (row["resolution"], row["resolved_file"], row.get("reexport_chain"))
                            for row in impact["importers"]}

                before = destinations()
                self.assertEqual(before["direct.ts"], ("direct_module", "target.ts", None))
                self.assertEqual(before["wrong.ts"], ("other_module", "other.ts", None))
                self.assertEqual(before["forwarded.ts"],
                                 ("reexport_module", "barrel.ts", [{"file": "barrel.ts", "line": 1}]))
                self.assertEqual(before["multi.ts"], ("reexport_module", "facade.ts",
                                 [{"file": "facade.ts", "line": 2}, {"file": "alias.ts", "line": 1}]))
                self.assertEqual(before["wildcard-user.ts"], ("other_module", "wildcard.ts", None))
                self.assertEqual(before["default-user.ts"], ("other_module", "barrel.ts", None))
                self.assertEqual(before["cyclic-user.ts"], ("other_module", "cycle1.ts", None))
                self.assertEqual(before["external.ts"], ("unresolved", None, None))
                (root / "barrel.ts").write_text("export { calculate } from './other';\n", encoding="utf-8")
                self.assertEqual(destinations()["forwarded.ts"], ("other_module", "barrel.ts", None))
                (root / "other.ts").unlink()
                self.assertEqual(destinations()["wrong.ts"], ("unresolved", None, None))

    def test_http_origin_and_body(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
        with patch.object(graphflow, "read", return_value={"text": "result", "provider": "test"}) as read:
            self.assertEqual(client.post("/api/graphflow/read", json={"query": "test"}).status_code, 200)
            self.assertEqual(client.post("/api/graphflow/read", json={"query": "test", "rootDir": "."}).status_code, 422)
            self.assertEqual(client.post("/api/graphflow/read", json={"query": "test"},
                                         headers={"Origin": "http://127.0.0.1:9000"}).status_code, 403)
            self.assertEqual(client.get("/api/graphflow/status", headers={"Host": "other.test"}).status_code, 403)
            read.assert_called_once_with("test", "", False)
        with patch.object(graphflow, "impact", return_value={"impacts": []}) as impact:
            body = {"target": "test.py#function"}
            self.assertEqual(client.post("/api/graphflow/impact", json=body).status_code, 200)
            self.assertEqual(client.post("/api/graphflow/impact", json=body, headers={"Origin": "null"}).status_code, 403)
            impact.assert_called_once_with(body["target"])


if __name__ == "__main__":
    unittest.main()
