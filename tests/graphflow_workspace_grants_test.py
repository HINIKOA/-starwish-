"""Discovered projects stay unreadable until the local panel approves them."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import config, graphflow, graphflow_routes
    from mcp_server import graphflow_context, graphflow_read

    origin = "http://127.0.0.1:17860"
    app = FastAPI()
    app.include_router(graphflow_routes.router)
    with tempfile.TemporaryDirectory(dir=ROOT) as tmp, patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": ""}):
        base = Path(tmp).resolve()
        roots = [base / "repo-a", base / "repo-b"]
        for root in roots:
            root.mkdir()
            subprocess.run(["git", "init", "-q", "--template="], cwd=root, check=True)
        with patch.object(config, "SOUL_DIR", base / "soul"), patch.object(graphflow, "read") as reader:
            reader.side_effect = lambda *a, **kw: {"text": graphflow.checked_workspace().name}
            client = TestClient(app, base_url=origin, client=("127.0.0.1", 50000))
            for root in [*roots, roots[0]]:
                pending = json.loads(graphflow_context("sample", rootDir=str(root)))
                assert pending["code_status"] == "workspace_authorization_required"
            assert not reader.called
            workspaces = client.get("/api/graphflow/workspaces").json()["workspaces"]
            ids = [item["id"] for item in workspaces]
            assert len(ids) == 2 and not any(item["approved"] for item in workspaces)
            decision = "/api/graphflow/workspaces/decision"
            body = {"ids": ids, "approve": True}
            assert client.post(decision, json=body, headers={"Origin": "https://other.invalid"}).status_code == 403
            assert client.post(decision, json=body).status_code == 403
            path = "/api/graphflow/read"
            assert client.post(path, params={"workspace_id": ids[0]}, json={"query": "sample"}).status_code == 403
            assert client.post(decision, json=body, headers={"Origin": origin}).status_code == 200
            for root, workspace_id in zip(roots, ids):
                assert json.loads(graphflow_read(query="sample", rootDir=str(root)))["text"] == root.name
                panel = client.post(path, params={"workspace_id": workspace_id}, json={"query": "sample"})
                assert panel.json()["text"] == root.name
            # Dynamic read grants do not become the process-wide editing workspace.
            try:
                graphflow.checked_workspace()
            except graphflow.GraphFlowError:
                pass
            else:
                raise AssertionError("a read grant leaked outside its request scope")
            assert client.post(decision, json={"ids": [ids[0]], "approve": False}, headers={"Origin": origin}).status_code == 200
            assert client.post(path, params={"workspace_id": ids[0]}, json={"query": "sample"}).status_code == 403
            assert json.loads(graphflow_read(query="sample", rootDir=str(roots[0])))["code_status"] == "workspace_authorization_required"

            policy = "/api/graphflow/workspaces/policy"
            assert not client.get("/api/graphflow/workspaces").json()["auto_authorize"]
            assert client.post(policy, json={"auto_authorize": True}).status_code == 403
            assert client.post(policy, json={"auto_authorize": True}, headers={"Origin": "https://other.invalid"}).status_code == 403
            existing, future = base / "repo-c", base / "repo-d"
            for root in (existing, future):
                root.mkdir()
                subprocess.run(["git", "init", "-q", "--template="], cwd=root, check=True)
            assert json.loads(graphflow_read(query="sample", rootDir=str(existing)))["code_status"] == "workspace_authorization_required"
            on = client.post(policy, json={"auto_authorize": True}, headers={"Origin": origin}).json()
            assert on["auto_authorize"]
            assert {item["name"]: item["authorization"] for item in on["workspaces"]} == {
                "repo-a": "excluded", "repo-b": "manual", "repo-c": "automatic",
            }
            for root in (existing, future):
                assert json.loads(graphflow_read(query="sample", rootDir=str(root)))["text"] == root.name
            assert json.loads(graphflow_read(query="sample", rootDir=str(roots[0])))["code_status"] == "workspace_authorization_required"
            off = client.post(policy, json={"auto_authorize": False}, headers={"Origin": origin}).json()
            assert not off["auto_authorize"]
            assert {item["name"]: item["authorization"] for item in off["workspaces"]} == {
                "repo-a": "excluded", "repo-b": "manual", "repo-c": "pending", "repo-d": "pending",
            }
            assert json.loads(graphflow_read(query="sample", rootDir=str(future)))["code_status"] == "workspace_authorization_required"
            assert json.loads(graphflow_read(query="sample", rootDir=str(roots[1])))["text"] == "repo-b"
    print("GraphFlow workspace discovery, local grants, read scoping and revoke passed")


if __name__ == "__main__":
    main()
