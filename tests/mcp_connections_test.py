"""Exercise real STDIO sessions against the panel's setup and status APIs."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def exercise(client, setup, fallback, modern_version=None):
    def status():
        return client.get("/api/mcp/status").json()

    async def wait(check):
        for _ in range(100):
            data = status()
            if check(data):
                return data
            await asyncio.sleep(0.1)
        raise AssertionError(data)

    async def connect():
        return await asyncio.create_subprocess_exec(
            setup["command"], *setup["args"], cwd=setup["cwd"], stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, env={**{k: v for k, v in os.environ.items()
                                                   if not k.startswith("STARWISH_GRAPHFLOW_")}, **setup["env"],
                                               "STARWISH_MCP_FALLBACK": str(int(fallback)), "UV_OFFLINE": "1"},
        )

    async def send(proc, method, params=None, request_id=None, *, expect_error=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if request_id is not None:
            msg["id"] = request_id
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()
        if request_id is not None:
            reply = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
            assert reply.get("id") == request_id and ("error" in reply) == expect_error, reply
            if expect_error:
                return reply["error"]
            return reply["result"]

    async def close(proc):
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

    assert status()["state"] == "disconnected"
    proc = await connect()
    try:
        await wait(lambda s: s["state"] == "partial" and s["pending"] == 1)
        name = "StarWish fallback test" if fallback else "StarWish SDK test"
        client_info = {"name": name, "version": "1.0"}
        meta = {}
        if modern_version:
            envelope = {"io.modelcontextprotocol/protocolVersion": modern_version,
                        "io.modelcontextprotocol/clientInfo": client_info}
            await send(proc, "tools/list", {"_meta": envelope}, 1, expect_error=True)
            assert status()["pending"] == 1 and status()["connected"] == 0
            meta = {"_meta": {**envelope, "io.modelcontextprotocol/clientCapabilities": {}}}
            listing = await send(proc, "tools/list", meta, 2)
        else:
            response = await send(proc, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                       "clientInfo": client_info}, 1)
            assert response["serverInfo"]["name"] == "starwish"
            await wait(lambda s: s["state"] == "partial" and s["sessions"][0]["name"] == name)
            await send(proc, "notifications/initialized")
        live = await wait(lambda s: s["state"] == "connected" and s["connected"] == 1)
        assert (live["sessions"][0]["name"], live["sessions"][0]["version"]) == (name, "1.0"), live
        if not modern_version:
            listing = await send(proc, "tools/list", request_id=2)
        tools = {tool["name"]: tool for tool in listing["tools"]}
        assert "recall_index" in tools
        contracts = {
            "graphflow_index": {"mode": (["incremental", "full"], "incremental")},
            "graphflow_plan": {
                "mode": (["simple", "insight"], "simple"),
                "intent": (["review", "change"], "review"),
            },
        }
        for name, fields in contracts.items():
            properties = tools[name]["inputSchema"]["properties"]
            for field, (choices, default) in fields.items():
                assert properties[field]["enum"] == choices, (name, field, properties[field])
                assert properties[field]["default"] == default, (name, field, properties[field])
        result = await send(proc, "tools/call", {**meta, "name": "recall_index", "arguments": {}}, 3)
        assert not result.get("isError"), result
        if "STARWISH_GRAPHFLOW_WORKSPACE" in setup["env"]:
            result = await send(proc, "tools/call", {"name": "graphflow_read",
                                "arguments": {"target": "fixture.py:1-2"}}, 4)
            assert not result.get("isError"), result
            assert any("return 417" in entry.get("text", "") for entry in result["content"]), result
        other = await connect()
        try:
            await wait(lambda s: s["state"] == "partial" and s["connected"] == 1 and s["pending"] == 1)
        finally:
            await close(other)
        await wait(lambda s: s["state"] == "connected")
    finally:
        await close(proc)
    await wait(lambda s: s["state"] == "disconnected")


def main():
    with tempfile.TemporaryDirectory(prefix="starwish-mcp-") as tmp:
        soul = Path(tmp) / "soul"
        os.environ["STARWISH_SOUL_DIR"] = str(soul)
        os.environ.pop("STARWISH_EXE", None)
        from fastapi.testclient import TestClient
        from app import graphflow, mcp_connections
        from app.server import app

        client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 50000))
        remote = TestClient(app, client=("192.0.2.10", 50000))
        for endpoint in ("setup", "status"):
            assert remote.get(f"/api/mcp/{endpoint}").status_code == 403
        setup = client.get("/api/mcp/setup").json()
        assert setup["type"] == "stdio" and setup["env"] == {"STARWISH_SOUL_DIR": str(soul)}
        for fallback in (False, True):
            asyncio.run(exercise(client, setup, fallback))
        try:
            from mcp_types.version import LATEST_MODERN_VERSION
        except ImportError:
            pass  # The legacy SDK has no per-request envelope.
        else:
            asyncio.run(exercise(client, setup, False, modern_version=LATEST_MODERN_VERSION))
        project_path = "/api/mcp/setup?include_project=true"
        assert remote.get(project_path).status_code == 403
        assert client.get(project_path, headers={"Origin": "https://other.invalid"}).status_code == 403
        with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(soul)}):
            unavailable = client.get(project_path)
            assert unavailable.status_code == 503 and unavailable.headers["cache-control"] == "no-store"
        git = shutil.which("git")
        if git:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run([git, "init", "-q", "--template="], cwd=repo, check=True)
            (repo / "fixture.py").write_text("def number():\n    return 417\n", encoding="utf-8")
            subprocess.run([git, "add", "--", "fixture.py"], cwd=repo, check=True)
            prefix = str(graphflow.engine_prefix().resolve())
            with patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(repo),
                                        "STARWISH_GRAPHFLOW_PREFIX": prefix,
                                        "STARWISH_GRAPHFLOW_OPERATOR_KEY": "not-exported",
                                        "STARWISH_GRAPHFLOW_EDIT_QUEUE": "1"}):
                assert client.get("/api/mcp/setup").json() == setup
                if graphflow.status()["available"]:
                    project = client.get(project_path)
                    assert project.status_code == 200 and project.headers["cache-control"] == "no-store", project.text
                    assert project.json()["env"] == {**setup["env"], "STARWISH_GRAPHFLOW_WORKSPACE": str(repo.resolve()),
                                                     "STARWISH_GRAPHFLOW_PREFIX": prefix}
                    for fallback in (False, True):
                        asyncio.run(exercise(client, project.json(), fallback))
                    print("Project opt-in: generated config and real SDK/fallback source reads passed")
                else:
                    assert client.get(project_path).status_code == 503
                    if os.environ.get("STARWISH_GRAPHFLOW_REQUIRE_ENGINE") == "1":
                        raise AssertionError(graphflow.status())
        with mcp_connections.Presence() as session:
            then = time.time() - mcp_connections.TTL - 1
            os.utime(session.path, (then, then))
            assert client.get("/api/mcp/status").json()["state"] == "disconnected"
    print("MCP SDK and fallback: generated setup, handshake, read-only call, mixed sessions, disconnect, expiry passed")


if __name__ == "__main__":
    main()
