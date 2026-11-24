import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.watch import extract
from app.watch.adapters import parse_codex
from app.watch.turns import Turn
from app.watch.watcher import Watcher


SOURCE = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
ENV = {"STARWISH_CODEX_DELEGATION_SOURCE": SOURCE, "STARWISH_CODEX_DELEGATION_TARGET": TARGET}
SCOPE = {"STARWISH_WATCH_CODEX_ONLY_SESSION": TARGET}


def event(kind, *, turn_id="turn-1", **fields):
    return json.dumps({"type": "event_msg", "payload": {
        "type": kind, "thread_id": TARGET, "turn_id": turn_id, **fields
    }}).encode() + b"\n"


def item(kind, content, **fields):
    return event("item_completed", item={"type": kind, "content": [{"type": "text", "text": content}], **fields})


def relay(source=SOURCE, namespace="codex_app", name="send_message_to_thread", turn_id="turn-1"):
    return event("item_completed", turn_id=turn_id, item={
        "type": "FunctionCallOutput", "namespace": namespace, "name": name,
        "output": f"<codex_delegation>\n  <source_thread_id>{source}</source_thread_id>\n"
                  "  <input>Fictional test preference\nsecond line</input>\n</codex_delegation>",
    })


def parse(data):
    return parse_codex("codex", data, 0, {"session_id": TARGET}, Path("fixture.jsonl"))


class CodexCaptureTest(unittest.TestCase):
    def test_modern_and_legacy_mirrors_count_once(self):
        data = (event("task_started") + event("user_message", message="Prompt") + item("UserMessage", "Prompt")
                + item("AgentMessage", "Progress", phase="commentary")
                + item("AgentMessage", "Answer", phase="final_answer")
                + event("agent_message", message="Answer") + event("task_complete"))
        turns, offset = parse(data)
        self.assertEqual(offset, len(data))
        self.assertEqual([(t.user_text, t.assistant_text, t.delegated) for t in turns], [("Prompt", "Answer", False)])

    def test_native_and_legacy_each_work_alone(self):
        for user, assistant in [
            (item("UserMessage", "Prompt"), item("AgentMessage", "Answer", phase="final_answer")),
            (event("user_message", message="Prompt"), event("agent_message", message="Answer")),
        ]:
            turns, _ = parse(user + assistant + event("task_complete"))
            self.assertEqual([(t.user_text, t.assistant_text, t.turn_complete) for t in turns],
                             [("Prompt", "Answer", True)])

    def test_only_opted_in_matching_active_relay_is_captured(self):
        start, end = event("task_started"), event("task_complete")
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(parse(start + relay() + end)[0])
        with patch.dict(os.environ, ENV):
            for rejected in [relay(source=TARGET), relay(namespace="other"), relay(name="other"), relay(turn_id="other")]:
                self.assertFalse(parse(start + rejected + end)[0])
            self.assertFalse(parse(relay() + end)[0])
            with patch.dict(os.environ, {"STARWISH_CODEX_DELEGATION_TARGET": "33333333-3333-4333-8333-333333333333"}):
                self.assertFalse(parse(start + relay() + end)[0])
            self.assertEqual(parse(start), ([], 0))
            self.assertEqual(parse(start + relay())[1], 0)
            self.assertFalse(parse(start + relay() + event("turn_aborted"))[0])
            data = start + relay() * 2 + item("AgentMessage", "Answer", phase="final_answer") + end
            turns, offset = parse(data)
            self.assertEqual(offset, len(data))
            self.assertEqual([(t.user_text, t.assistant_text, t.delegated, t.turn_complete) for t in turns],
                             [("Fictional test preference\nsecond line", "Answer", True, True)])

    def test_idle_does_not_store_pending_delegation(self):
        with patch.dict(os.environ, ENV), patch.object(extract, "process_turn") as process, \
             patch("app.watch.watcher.State"), patch("app.watch.watcher.config.load_config", return_value={"watch": {}}):
            turns, safe = parse(event("task_started") + relay())
            Watcher()._process_turns(turns, None, force=True, end_safe=safe)
        process.assert_not_called()

    def test_single_session_scope_filters_sources_and_payload_fallback(self):
        cfg = {"watch": {"clients": {"codex": {"enabled": True}, "claude": {"enabled": True}}}}
        with patch.dict(os.environ, SCOPE), patch("app.watch.watcher.State"), \
             patch("app.watch.watcher.config.load_config", return_value=cfg), \
             patch("app.watch.watcher.adapters.find_session_file", return_value=None) as find:
            watcher = Watcher()
            allowed = Path(f"rollout-2026-01-01T10-00-00-{TARGET}.jsonl")
            other = allowed.with_name(allowed.name.replace(TARGET, SOURCE))
            self.assertEqual(watcher.enabled_clients(), ["codex"])
            self.assertTrue(watcher._within_file_scope("codex", allowed))
            self.assertFalse(watcher._within_file_scope("codex", other))
            self.assertFalse(watcher._within_file_scope("claude", allowed))
            self.assertFalse(watcher.notify("codex", {"session_id": SOURCE})["ok"])
            find.assert_not_called()
            self.assertFalse(watcher.notify("codex", {
                "session_id": TARGET, "prompt": "Fixture", "last_assistant_message": "Fixture answer"
            })["ok"])
            self.assertTrue(watcher.q.empty())

    def test_scope_rejects_rollout_with_conflicting_session_metadata(self):
        with patch.dict(os.environ, SCOPE), patch("app.watch.watcher.State"), \
             patch("app.watch.watcher.config.load_config", return_value={"watch": {}}), TemporaryDirectory() as tmp:
            watcher = Watcher()
            path = Path(tmp) / f"rollout-2026-01-01T10-00-00-{TARGET}.jsonl"
            path.write_bytes(json.dumps({"type": "session_meta", "payload": {"id": SOURCE}}).encode() + b"\n"
                             + item("UserMessage", "Fixture") + event("task_complete"))
            watcher.state.files = {str(path): {"c": "codex", "o": 0, "meta": {}}}
            with patch.object(watcher, "_llm_ok", return_value=True), patch.object(watcher, "_process_turns") as process:
                self.assertEqual(watcher.process_file(str(path)), 0)
            process.assert_not_called()

    def test_delegated_memory_does_not_update_profile_or_learn_skills(self):
        t = Turn("codex", TARGET, user_text="Sample", delegated=True)
        output = json.dumps({"items": [
            {"kind": "preference", "text": "Fictional taste", "importance": 5},
            {"kind": "task", "text": "Do not change a real task"},
        ]})
        with patch.object(extract.store, "open_tasks", return_value=[]) as open_tasks, \
             patch.object(extract, "llm", return_value=output) as llm, \
             patch.object(extract, "log_event"), \
             patch.object(extract.stars, "add_star", return_value={}) as star, \
             patch.object(extract.core, "create_proposal") as core, \
             patch.object(extract.store, "upsert_fragment") as task, \
             patch("app.evolve.review.on_turn") as learn:
            result = extract.process_turn(t, {"watch": {}})
        self.assertEqual(result["stars"], 1)
        self.assertEqual(star.call_args.kwargs["source"], "watch:codex:delegated")
        prompt = llm.call_args.args[0][0]["content"]
        self.assertIn("【虚构测试输入】Sample", prompt)
        self.assertNotIn("【主人】", prompt)
        self.assertIn("科学记忆的 category", prompt)
        open_tasks.assert_not_called()
        core.assert_not_called()
        task.assert_not_called()
        learn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
