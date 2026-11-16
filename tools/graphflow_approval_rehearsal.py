"""Run a disposable, manually approved StarWish edit/test/rollback rehearsal."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if __package__:
    from .graphflow_codex_probe import probe, require
else:
    from graphflow_codex_probe import probe, require

ORIGINAL = b"def calculate():\n    return 42\n"
UPDATED = ORIGINAL.replace(b"return 42", b"return 43")
TEST = ("import unittest\nfrom pathlib import Path\nfrom fixture import calculate\n\n"
        "class RehearsalTest(unittest.TestCase):\n"
        "    def test_approved_change(self):\n"
        f"        self.assertEqual(Path('fixture.py').read_bytes(), {UPDATED!r})\n"
        "        self.assertEqual(calculate(), 43)\n")


def evidence(edit_db, test_db, preview_id, source, *, task=None):
    with closing(sqlite3.connect(edit_db.as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        events = db.execute("SELECT state,at FROM edit_events WHERE preview_id=? ORDER BY seq", (preview_id,)).fetchall()
        edit = db.execute("SELECT path,base,result,state FROM edits WHERE id=?", (preview_id,)).fetchone()
    if not events or events[-1][0] != "rolled_back":
        return False
    states, stamps = zip(*events)
    require(states == ("pending", "applying", "applied", "rolling_back", "rolled_back")
            and all(a <= b for a, b in zip(stamps, stamps[1:])), "Missing edit/rollback audit order.")
    require(edit == ("fixture.py", hashlib.sha256(ORIGINAL).hexdigest(), hashlib.sha256(UPDATED).hexdigest(), "rolled_back"),
            "Audit does not describe the rehearsal fixture and expected hashes.")
    require(source.read_bytes() == ORIGINAL, "Rollback did not restore the original bytes.")
    require(test_db.is_file(), "No manual test was started before rollback.")
    with closing(sqlite3.connect(test_db.as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        # The column is internal; task-enabled rehearsals cross-check actual run identifiers.
        results = db.execute(f"""SELECT {'id' if task else '1'} FROM runs
                              WHERE profile='rehearsal' AND state='passed' AND exit_code=0
                              AND started>=? AND started+duration_ms/1000.0<=?""",
                             (stamps[2], stamps[3] + .001)).fetchall()
    require(bool(results), "No passing controlled test between apply and rollback.")
    if task:
        task_db, task_id = task
        with closing(sqlite3.connect(task_db.as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
            row = db.execute("SELECT state,revision FROM tasks WHERE id=?", (task_id,)).fetchone()
            require(row == ("pending", 0), "Rehearsal task changed state or revision.")
            linked = db.execute("""SELECT run_id FROM task_verifications
                                   WHERE task_id=? AND profile='rehearsal' AND state='passed'
                                   AND exit_code=0 AND recorded_at BETWEEN ? AND ?""",
                                (task_id, stamps[2], stamps[3])).fetchall()
        require(bool(set(linked) & set(results)), "Passing rehearsal test was not linked to the selected task.")
    return True


def run(codex: str, seconds: int, check_only: bool) -> None:
    git = shutil.which("git")
    require(bool(git), "Git is required.")
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    host_env = {key: value for key, value in os.environ.items()
                if not key.upper().startswith(("STARWISH_", "CODEX_", "OPENAI_", "GIT_"))}
    engine_base = Path(host_env.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    with tempfile.TemporaryDirectory(prefix="starwish-approval-") as directory, socket.socket() as listener:
        root = Path(directory).resolve()
        repo, state, soul, codex_home = (root / part for part in ("repo", "state", "soul", "codex"))
        repo.mkdir()
        codex_home.mkdir()
        source = repo / "fixture.py"
        source.write_bytes(ORIGINAL)
        (repo / "fixture_test.py").write_text(TEST, encoding="utf-8")
        for args in (("init", "-q", "--template="), ("add", "--", "fixture.py", "fixture_test.py"),
                     ("-c", "core.hooksPath=.git", "-c", "commit.gpgsign=false", "-c", "user.name=StarWish",
                      "-c", "user.email=rehearsal@example.test", "commit", "-qm", "Rehearsal baseline")):
            subprocess.run([git, *args], cwd=repo, env=host_env, check=True, capture_output=True, timeout=15)
        os.environ["STARWISH_SOUL_DIR"] = str(soul)
        from app import graphflow

        prefix = os.environ.get("STARWISH_GRAPHFLOW_PREFIX",
                                str(engine_base / "StarWish" / f"graphflow-{graphflow.VERSION}"))
        basic = {"STARWISH_SOUL_DIR": str(soul), "STARWISH_GRAPHFLOW_WORKSPACE": str(repo),
                 "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1", "STARWISH_GRAPHFLOW_PREFIX": prefix,
                 "STARWISH_GRAPHFLOW_TASKS": "1",
                 "LOCALAPPDATA": str(state), "XDG_STATE_HOME": str(state), "PYTHONUTF8": "1"}
        profiles = {"rehearsal": {"argv": [sys._base_executable, "-B", "-m", "unittest", "fixture_test", "-v"],
                                  "paths": ["fixture.py"], "timeout": 15}}
        key = secrets.token_urlsafe(24)
        panel_env = {**basic, "STARWISH_GRAPHFLOW_OPERATOR_KEY": key,
                     "STARWISH_GRAPHFLOW_TEST_PROFILES": json.dumps(profiles)}
        os.environ.update(panel_env)
        from app.graphflow_edit_queue import state_path
        from app.graphflow_routes import router

        app = FastAPI()
        app.include_router(router)
        app.mount("/static", StaticFiles(directory=ROOT / "web"))

        quote = lambda value: json.dumps(value, ensure_ascii=False)
        settings = ['web_search = "disabled"', "[analytics]", "enabled = false",
                    "[mcp_servers.starwish]", f"command = {quote(sys.executable)}",
                    f"args = [{quote(str(ROOT / 'mcp_server.py'))}]", f"cwd = {quote(str(ROOT))}",
                    "[mcp_servers.starwish.env]", *(f"{name} = {quote(value)}" for name, value in basic.items())]
        (codex_home / "config.toml").write_text("\n".join(settings) + "\n", encoding="utf-8")
        codex_env = {**host_env, **basic, "CODEX_HOME": str(codex_home), "NO_PROXY": "127.0.0.1,localhost"}

        def queue_fixture(call):
            proposal = json.loads(call("graphflow_edit_preview", path="fixture.py", start_line=2, end_line=2,
                                       expected="    return 42", replacement="    return 43"))
            preview_id = proposal.get("preview_id", "")
            require(proposal.get("queued") is True and re.fullmatch(r"[0-9a-f]{32}", preview_id),
                    "Codex MCP did not place the preview in the shared queue.")
            workflow = json.loads(call("graphflow_workflow_preview", name="change-review"))
            task = json.loads(call("graphflow_task_create", request_id="approval-rehearsal", name="change-review",
                                   plan_sha256=workflow["plan_sha256"]))
            require(task.get("state") == "pending" and re.fullmatch(r"[0-9a-f]{32}", task.get("id", "")),
                    "Codex MCP did not create a rehearsal task.")
            return preview_id, task["id"]

        preview_id, task_id = probe(codex, repo, repo, environment=codex_env, on_connected=queue_fixture)
        require(source.read_bytes() == ORIGINAL, "Preview unexpectedly changed the fixture.")
        edit_db = state_path(repo, create=False)
        test_db = state_path(repo, "-tests", create=False)
        task_db = state_path(repo, "-tasks", create=False)
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            until = time.monotonic() + 10
            while not server.started and worker.is_alive() and time.monotonic() < until:
                time.sleep(.05)
            require(server.started, "The loopback rehearsal panel did not start.")
            opener = build_opener(ProxyHandler({}))
            with opener.open(url + "/graphflow", timeout=3) as response:
                page = response.read()
            require(all(marker in page for marker in (b'id="gf-approval-form"', b'id="gf-test-form"', b'id="gf-test-task"')),
                    "The rehearsal page did not expose operator forms and task linking.")
            queue_url = url + "/api/graphflow/edit/queue"
            try:
                with opener.open(queue_url, timeout=3):
                    raise RuntimeError("Approval queue responded without a key.")
            except HTTPError as error:
                with error:
                    require(error.code == 403, "Unexpected status for keyless approval queue.")
            with opener.open(Request(queue_url, headers={"X-StarWish-Operator-Key": key}), timeout=3) as response:
                items = json.load(response)
            require(any(item["preview_id"] == preview_id and item["state"] == "pending" for item in items),
                    "The web server did not see the pending MCP preview.")
            with opener.open(f"{url}/api/graphflow/tasks/detail?task_id={task_id}", timeout=3) as response:
                task = json.load(response)
            require(task["id"] == task_id and task["state"] == "pending" and task["test_verifications"] == [],
                    "The web server did not see the pending task without verifications.")
            require(not test_db.exists(), "The rehearsal unexpectedly created tests before approval.")
            if check_only:
                print("PASS: Codex MCP preview and task reached the panel; nothing applied or run.")
                return
            print(f"Panel: {url}/graphflow\nTemporary key: {key}\nPreview: {preview_id[:8]}\nTask: {task_id}\n"
                  "Apply the edit, approve and run 'rehearsal' with this task ID, then roll back.\n"
                  "The temporary panel closes after success, timeout or Ctrl+C.", flush=True)
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                if evidence(edit_db, test_db, preview_id, source, task=(task_db, task_id)):
                    print("PASS: applied, task-linked test passed, rollback restored the original bytes.")
                    return
                time.sleep(.5)
            raise RuntimeError("Timed out waiting for manual approval, test and rollback.")
        finally:
            server.should_exit = True
            worker.join(8)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=900, choices=range(30, 1801), metavar="30..1800")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        run(str(args.codex.resolve(strict=True)), args.seconds, args.check_only)
    except (RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError, KeyboardInterrupt) as error:
        parser.exit(1, f"Rehearsal stopped: {error}\n")
