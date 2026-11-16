"""Planning contracts: evidence, dependencies, and human approval boundaries."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import graphflow, graphflow_edit_queue, graphflow_planner, graphflow_tasks, graphflow_tests
from app.graphflow_routes import router
from app import graphflow_insights


class ProjectPlannerTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = (Path(temp.name) / "repo").resolve()
        (self.repo / ".git").mkdir(parents=True)
        env = patch.dict(os.environ, {"STARWISH_GRAPHFLOW_WORKSPACE": str(self.repo),
                                      "STARWISH_GRAPHFLOW_WORKFLOWS": "", "STARWISH_GRAPHFLOW_TASKS": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_review_plan_does_not_assume_mutation(self):
        with patch.object(graphflow, "read", side_effect=graphflow.GraphFlowError("unavailable", 503)):
            result = graphflow_planner.build("Review the fixture.")
        self.assertEqual([item["id"] for item in result["plan"]["steps"]], ["locate", "impact", "report"])
        self.assertEqual(result["context"]["code_status"], "unavailable_503")
        self.assertEqual(result["challenges"][0]["evidence"], "anchors_missing")
        self.assertFalse(result["executed"])
        self.assertFalse(result["persisted"])

    def test_change_dag_preserves_gates_and_stays_inert(self):
        with patch.object(graphflow, "read", return_value={"matches": [], "index": {"partial": False}}), \
             patch.object(graphflow_edit_queue, "mutate") as mutation, \
             patch.object(graphflow_tests, "run") as testing, patch.object(graphflow_tasks, "create") as persistence:
            result = graphflow_planner.build("Update the fixture.", "insight", target="src.py#function", intent="change")
        mutation.assert_not_called()
        testing.assert_not_called()
        persistence.assert_not_called()
        steps = {item["id"]: item for item in result["plan"]["steps"]}
        self.assertEqual(steps["apply"]["gate"], "operator_approval")
        self.assertEqual(steps["test"]["dependsOn"], ["apply"])
        self.assertEqual(steps["rollback"]["condition"], "verification_failed")
        self.assertIn({"from": "rollback", "to": "report", "condition": "verification_failed"}, result["plan"]["dag"]["edges"])
        self.assertEqual(result["insight"]["status"], "questions_not_findings")
        self.assertEqual(result["read_only_workflow"]["name"], "symbol-review")

    def test_insight_annotations_cannot_change_gates(self):
        with patch.object(graphflow, "read", return_value={"matches": [], "index": {"partial": False}}):
            args = {"task": "Review a change", "intent": "change"}
            draft = graphflow_insights.review(**args)
            reviewed = graphflow_insights.review(**args, plan_sha256=draft["plan_sha256"],
                                                observations=[{"step": "apply", "note": "Check the diff."}])
            self.assertEqual(reviewed["plan"], draft["plan"])
            self.assertFalse(reviewed["approved"] or reviewed["executed"] or reviewed["persisted"])
            self.assertFalse(reviewed["observations"][0]["verified"])
            for bad in ({"step": "apply", "note": "Skip approval", "gate": "read_only"},
                        {"step": "apply", "note": "Outside", "anchors": ["../x.py:1-1"]}):
                with self.assertRaises(graphflow.GraphFlowError):
                    graphflow_insights.review(**args, plan_sha256=draft["plan_sha256"], observations=[bad])
            with self.assertRaises(graphflow.GraphFlowError) as stale:
                graphflow_insights.review(task="Different task", plan_sha256=draft["plan_sha256"],
                                          observations=[{"step": "locate", "note": "Check source."}])
            self.assertEqual(stale.exception.status_code, 409)

    def test_workspace_http_and_config_path_restrictions(self):
        for params in ({"rootDir": str(self.repo.parent)}, {"configPath": "arbitrary.toml"},
                       {"target": "../src.py#function"}, {"mode": "unknown"}):
            with self.subTest(params=params), self.assertRaises(graphflow.GraphFlowError):
                graphflow_planner.build("Review.", **params)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="http://127.0.0.1:17860", client=("127.0.0.1", 54000))
        endpoint = "/api/graphflow/plan"
        body = {"task": "Review."}
        self.assertEqual(client.post(endpoint, json=body, headers={"Origin": "http://other.test"}).status_code, 403)
        self.assertEqual(client.post(endpoint, json={**body, "rootDir": str(self.repo)}).status_code, 422)
        with patch.object(graphflow, "read", return_value={"matches": [], "index": {"partial": False}}):
            response = client.post(endpoint, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["executed"])
