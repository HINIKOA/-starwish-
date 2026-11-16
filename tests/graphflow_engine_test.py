import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tools import build_windows, graphflow_codex, graphflow_codex_probe, graphflow_engine as engine


class GraphFlowEngineTest(unittest.TestCase):
    def test_external_embedding_assets_copy_only_required_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            models = root / "external-models"
            source = models / "embed" / "small"
            source.mkdir(parents=True)
            (source / "model.onnx").write_bytes(b"onnx fixture")
            with self.assertRaisesRegex(SystemExit, "--models-dir"):
                build_windows.embedding_assets(models)
            (source / "tokenizer.json").write_bytes(b"tokenizer fixture")
            (source / "not-for-release.txt").write_bytes(b"external extra")
            stage = root / "app"
            stage.mkdir()
            with patch.dict(os.environ, {"STARWISH_MODELS_DIR": str(models)}):
                build_windows.copy_app(stage)
            built = stage / "models" / "embed" / "small"
            self.assertEqual(sorted(path.name for path in built.iterdir()), ["model.onnx", "tokenizer.json"])
            self.assertEqual((built / "model.onnx").read_bytes(), b"onnx fixture")

    def test_onefile_payload_preserves_engine_and_excludes_folder_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            prefix = app / "engines" / "graphflow-3.9.2"
            prefix.mkdir(parents=True)
            (prefix / "THIRD-PARTY.json").write_bytes(b"[]")
            (app / f"{build_windows.APP_NAME}.exe").write_bytes(b"folder executable")
            stub = b"MZ-test-stub"
            artifact = build_windows.pack_onefile(app, root / "test.exe", "0.1.0", stub)
            trailer = build_windows.read_trailer(artifact)
            self.assertEqual(trailer["offset"], len(stub))
            self.assertEqual(trailer["total"], len(stub) + trailer["size"] + 64)
            with artifact.open("rb") as stream:
                self.assertEqual(stream.read(len(stub)), stub)
                payload = stream.read(trailer["size"])
            self.assertEqual(trailer["tag"], "0.1.0-" + hashlib.sha256(payload).hexdigest()[:10])
            with zipfile.ZipFile(artifact) as archive:
                self.assertEqual(archive.namelist(), ["engines/graphflow-3.9.2/THIRD-PARTY.json"])
                self.assertEqual(archive.read(archive.namelist()[0]), b"[]")

    def test_configuration_prefers_portable_python_over_uv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "repo" / ".git").mkdir(parents=True)
            runtime = root / "runtime"
            runtime.mkdir()
            python = runtime / "python.exe"
            python.touch()
            (runtime / "python312._pth").touch()
            with patch.object(graphflow_codex, "ROOT", root), patch.object(graphflow_codex.shutil, "which") as which:
                config = graphflow_codex.preview(root / "repo", root / "soul")
                which.assert_not_called()
            self.assertIn("command = " + graphflow_codex._toml(str(python)), config)
            self.assertIn("args = [" + graphflow_codex._toml(str(root / "mcp_server.py")) + "]", config)

    @unittest.skipUnless(os.name == "nt" and os.environ.get("STARWISH_GRAPHFLOW_PORTABLE_RUNTIME"),
                         "requires an existing Windows embeddable Python runtime with packaged dependencies")
    def test_real_portable_configuration_calls_bundled_engine_through_codex(self):
        donor = Path(os.environ["STARWISH_GRAPHFLOW_PORTABLE_RUNTIME"]).resolve(strict=True)
        codex = os.environ.get("STARWISH_GRAPHFLOW_CODEX_BINARY") or shutil.which("codex.exe")
        self.assertIsNotNone(codex)
        self.assertTrue((donor / "python.exe").is_file())
        with tempfile.TemporaryDirectory(prefix="starwish portable rpc ") as directory:
            root = Path(directory).resolve()
            portable, repo, soul, home = (root / name for name in ("portable app", "repo", "soul", "codex"))
            for folder in (portable, repo, soul, home):
                folder.mkdir()
            build_windows.copy_app(portable)
            engine.bundle_windows(portable, offline=True)
            shutil.copytree(donor, portable / "runtime")
            original = b"def verify_portable():\n    return 42\n"
            source = repo / "fixture.py"
            source.write_bytes(original)
            for args in (("init", "-q", "--template="), ("add", "--", "fixture.py")):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=15)
            python = portable / "runtime" / "python.exe"
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith(
                ("CODEX_", "STARWISH_", "OPENAI_"))}
            env.update(CODEX_HOME=str(home), LOCALAPPDATA=str(root / "state"), PYTHONUTF8="1")
            rendered = subprocess.run([str(python), str(portable / "tools" / "graphflow_codex.py"), "preview",
                                       "--workspace", str(repo), "--soul", str(soul)],
                                      cwd=portable, env=env, check=True, capture_output=True, encoding="utf-8", timeout=15)
            self.assertIn("command = " + graphflow_codex._toml(str(python)), rendered.stdout)
            self.assertNotIn("--with-requirements", rendered.stdout)
            (home / "config.toml").write_text(rendered.stdout, encoding="utf-8")
            report = graphflow_codex_probe.probe(codex, repo, repo, "fixture.py#verify_portable",
                                                project_only=True, preview_line=2, environment=env)
            self.assertEqual(report["starwish"], "connected")
            self.assertIn("edit-diff-without-receipt", report["checks"])
            self.assertIn("read-only-macro-replay", report["checks"])
            self.assertFalse(report["model_turn_started"] or report["memory_tools_called"])
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse((root / "state" / "StarWish" / f"graphflow-{engine.graphflow.VERSION}").exists())
            cc, windres = (os.environ.get(key) for key in ("STARWISH_WINDOWS_CC", "STARWISH_WINDOWS_WINDRES"))
            if cc or windres:
                self.assertTrue(cc and windres)
                build_windows.launcher(portable, cc=cc, windres=windres)
                single = build_windows.onefile(portable, root / "single file.exe", engine.graphflow.VERSION,
                                               cc=cc, windres=windres)
                failed = subprocess.run([str(single), "--mcp"], cwd=portable,
                                        env={**env, "STARWISH_RUNTIME_CACHE_ROOT": "relative-cache"},
                                        capture_output=True, timeout=10, encoding="utf-8")
                self.assertEqual((failed.returncode, failed.stdout), (4, ""))
                self.assertTrue(failed.stderr.strip())
                folder_exe = portable / f"{build_windows.APP_NAME}.exe"
                cache = root / "extraction"
                marker = cache / "StarWish" / "runtime" / "old-fixture" / "keep.txt"
                marker.parent.mkdir(parents=True)
                marker.write_bytes(b"keep old cache")
                def cold_start(_):
                    return subprocess.run([str(single), "--mcp"], cwd=portable,
                                          env={**env, "STARWISH_RUNTIME_CACHE_ROOT": str(root / "direct-extraction"),
                                               "STARWISH_SOUL_DIR": str(soul), "STARWISH_MCP_FALLBACK": "1"},
                                          input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                                          capture_output=True, timeout=70, encoding="utf-8")

                with ThreadPoolExecutor(max_workers=2) as pool:
                    for startup in pool.map(cold_start, range(2)):
                        self.assertEqual(startup.returncode, 0, startup.stderr)
                        self.assertIn("graphflow_read", startup.stdout, startup.stderr)
                tag = build_windows.read_trailer(single)["tag"]
                extractor = cache / "StarWish" / "runtime" / tag
                self.assertFalse(extractor.exists())
                for exe in (folder_exe, single):
                    with self.subTest(exe=exe.name):
                        with exe.open("rb") as stream:
                            self.assertEqual(stream.read(2), b"MZ")
                        config = graphflow_codex.preview(repo, soul, launcher_exe=exe)
                        self.assertIn('args = ["--mcp"]', config)
                        self.assertIn("startup_timeout_sec = 90", config)
                        config += "\nSTARWISH_RUNTIME_CACHE_ROOT = " + graphflow_codex._toml(str(cache)) + "\n"
                        (home / "config.toml").write_text(config, encoding="utf-8")
                        with tempfile.TemporaryFile() as stderr:
                            try:
                                exe_report = graphflow_codex_probe.probe(codex, repo, repo, "fixture.py#verify_portable",
                                                                        project_only=True, environment=env,
                                                                        startup_wait=105, stderr=stderr)
                            except RuntimeError as exc:
                                stderr.seek(max(0, stderr.tell() - 6000))
                                self.fail(f"{exc}\n{stderr.read().decode('utf-8', errors='replace')}")
                        self.assertIn("source-range", exe_report["checks"])
                        self.assertIn("read-only-macro-replay", exe_report["checks"])
                self.assertTrue((extractor / ".ok").is_file())
                self.assertTrue((extractor / "engines" / f"graphflow-{engine.graphflow.VERSION}" / "THIRD-PARTY.json").is_file())
                self.assertEqual(marker.read_bytes(), b"keep old cache")
                self.assertEqual(source.read_bytes(), original)

    def test_packaged_prefix_precedence_and_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app = root / "portable"
            bundled = app / "engines" / f"graphflow-{engine.graphflow.VERSION}"
            bundled.mkdir(parents=True)
            build_windows.copy_graphflow_tools(app)
            scripts = app / "tools"
            self.assertTrue((scripts / "graphflow_engine.py").is_file())
            self.assertTrue((scripts / "graphflow_codex_probe.py").is_file())
            self.assertEqual(engine.locked_runtime(scripts / "graphflow_runtime"), engine.locked_runtime())
            with patch.object(engine.graphflow.config, "APP_DIR", app), patch.dict(os.environ, {
                    "STARWISH_GRAPHFLOW_PREFIX": "", "LOCALAPPDATA": str(root / "state")
            }):
                self.assertEqual(engine.graphflow.engine_prefix(), bundled)
                with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_PREFIX": str(root / "override")}):
                    self.assertEqual(engine.graphflow.engine_prefix(), root / "override")
                bundled.rmdir()
                self.assertEqual(engine.graphflow.engine_prefix(), root / "state" / "StarWish" / bundled.name)

    @unittest.skipUnless(os.name == "nt" and os.environ.get("STARWISH_GRAPHFLOW_BUNDLE_TEST") == "1",
                         "optional Windows x64 npm cache integration")
    def test_real_windows_bundle_checks_engine_and_retains_license_files(self):
        with tempfile.TemporaryDirectory(prefix="starwish bundle ") as directory:
            portable = Path(directory).resolve()
            build_windows.copy_app(portable)
            report = engine.bundle_windows(portable, offline=True)
            bundled = portable / "engines" / f"graphflow-{engine.graphflow.VERSION}"
            command = [sys.executable, str(portable / "tools" / "graphflow_engine.py"), "check"]
            env = {**os.environ, "LOCALAPPDATA": str(portable / "test-user-state"), "STARWISH_GRAPHFLOW_PREFIX": "",
                   "STARWISH_SOUL_DIR": str(portable / "test-soul")}
            check = subprocess.run(command, env=env, cwd=portable, check=True,
                                   capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(json.loads(check.stdout)["slice"], "passed")
            self.assertFalse((portable / "test-user-state").exists())
            licenses = json.loads((bundled / "THIRD-PARTY.json").read_bytes())
            self.assertEqual(len(licenses), report["installed_packages"])
            graph = next(item for item in licenses if item["name"] == engine.PACKAGE)
            self.assertEqual((graph["version"], graph["license"]), (engine.graphflow.VERSION, "Apache-2.0"))
            for item in licenses:
                self.assertTrue(all((bundled / item["path"] / file).is_file() for file in item["notice_files"]))

    def test_existing_directory_is_checked_without_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "existing"
            prefix.mkdir()
            marker = prefix / "keep"
            marker.write_bytes(b"existing")
            with patch.object(engine, "check", side_effect=RuntimeError("broken engine")), \
                 patch.object(engine, "npm_command") as npm:
                with self.assertRaisesRegex(RuntimeError, "broken engine"):
                    engine.install(prefix)
                npm.assert_not_called()
            self.assertEqual(marker.read_bytes(), b"existing")

    def test_smoke_failure_leaves_no_published_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            prefix = parent / "new"
            with patch.object(engine, "npm_command", return_value=["npm"]), \
                 patch.object(subprocess, "run") as npm, \
                 patch.object(engine, "check", side_effect=RuntimeError("bad parser")):
                with self.assertRaisesRegex(RuntimeError, "bad parser"):
                    engine.install(prefix, offline=True)
                args = npm.call_args.args[0]
                self.assertIn("--ignore-scripts", args)
                self.assertIn("--offline", args)
                self.assertEqual(args[:2], ["npm", "ci"])
            self.assertEqual(list(parent.iterdir()), [])

    def test_invalid_registry_is_rejected(self):
        manifest, lock, count = engine.locked_runtime()
        self.assertGreater(count, 1)
        data = json.loads(lock)
        data["packages"]["node_modules/" + engine.PACKAGE]["resolved"] = "https://example.test/runtime.tgz"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_bytes(manifest)
            (root / "package-lock.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Unpinned npm lock entry"):
                engine.locked_runtime(root)
