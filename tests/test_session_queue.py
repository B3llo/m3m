"""Tests for asynchronous SessionEnd queueing and handoff acknowledgement."""

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from agent_m3m.cli import _process_session_queue, _enqueue_session_event, main
from agent_m3m.config import Config
from agent_m3m.handoff import Handoff, HandoffStore
from agent_m3m.models import MemoryItem
from agent_m3m.queue import SessionQueue
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore


class TestSessionQueue(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = Config(
            global_path=root / "global_memory",
            project_path=root / "project_memory",
        )
        self.store = MemoryStore(self.config)
        self.retriever = MemoryRetriever(self.store, self.config)
        self.store.init_global()
        self.store.init_project()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_enqueue_is_atomic_bounded_and_sanitized(self):
        queue = SessionQueue(self.config)
        entry = queue.enqueue(
            payload={
                "transcript": "Use sk-proj-1234567890abcdef1234567890abcdef only locally.",
                "nested": {"token": "ghp_1234567890abcdef1234567890abcdef"},
            },
            raw_input="Authorization: Bearer abcdefghijklmnop",
            session_id="session-1",
        )

        self.assertIsNotNone(entry)
        self.assertEqual(queue.counts(), {"pending": 1, "processing": 0, "failed": 0, "done": 0})
        files = list((self.config.project_dir / "sessions" / "queue" / "pending").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("sk-proj-1234567890abcdef", files[0].read_text(encoding="utf-8"))
        self.assertNotIn("ghp_1234567890abcdef", files[0].read_text(encoding="utf-8"))
        self.assertNotIn("abcdefghijklmnop", files[0].read_text(encoding="utf-8"))
        self.assertFalse(list(files[0].parent.glob("*.tmp")))

    def test_hook_enqueue_does_not_invoke_extractor(self):
        with mock.patch("agent_m3m.cli.MemoryExtractor") as extractor:
            entry = _enqueue_session_event(
                self.config,
                json.dumps({"transcript": "Prefer local tooling.", "session_id": "s-2"}),
                {"transcript": "Prefer local tooling.", "session_id": "s-2"},
            )

        self.assertIsNotNone(entry)
        extractor.assert_not_called()
        self.assertEqual(len(SessionQueue(self.config).list_entries("pending")), 1)

    def test_save_session_end_cli_path_only_enqueues(self):
        payload = json.dumps({"transcript": "USER: Keep the hook fast.", "session_id": "cli-session"})
        with mock.patch("agent_m3m.cli.Config", return_value=self.config), \
             mock.patch("agent_m3m.cli.MemoryExtractor") as extractor, \
             mock.patch("agent_m3m.cli.log_hook_event"), \
             mock.patch.object(sys, "argv", ["m3m", "save", "--stdin", "--session-end", "--hook-output"]), \
             mock.patch.object(sys, "stdin", io.StringIO(payload)):
            main()

        extractor.assert_not_called()
        entries = SessionQueue(self.config).list_entries("pending")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].session_id, "cli-session")

    def test_codex_jsonl_hook_reaches_queue_processor_as_raw_transcript(self):
        rollout = "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "rollout-1"}}),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Keep the deploy command local."}],
                        },
                    }
                ),
            ]
        )

        with mock.patch("agent_m3m.cli.Config", return_value=self.config), \
             mock.patch("agent_m3m.cli.log_hook_event"), \
             mock.patch.object(sys, "argv", ["m3m", "save", "--stdin", "--session-end", "--hook-output"]), \
             mock.patch.object(sys, "stdin", io.StringIO(rollout)):
            main()

        queued = SessionQueue(self.config).list_entries("pending")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload, {})
        self.assertEqual(queued[0].raw_input, rollout)

        with mock.patch("agent_m3m.cli.MemoryExtractor") as extractor_class:
            extractor_class.return_value.extract_from_transcript.return_value = []
            report = _process_session_queue(self.config, self.store, self.retriever)

        self.assertEqual(report.processed, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(
            extractor_class.return_value.extract_from_transcript.call_args.args[0],
            rollout,
        )

    def test_hook_context_disables_llm_reranking_without_mutating_normal_config(self):
        self.config.llm_rerank = True
        with mock.patch("agent_m3m.cli.Config", return_value=self.config), \
             mock.patch("agent_m3m.cli.ContextBuilder") as builder_class, \
             mock.patch("agent_m3m.cli.log_hook_event"), \
             mock.patch.object(sys, "argv", ["m3m", "context", "--hook-output"]):
            builder_class.return_value.handoffs.load.return_value = None
            builder_class.return_value.build_context.return_value = "local context"
            main()

        _, hook_retriever, hook_config = builder_class.call_args.args
        self.assertFalse(hook_config.llm_rerank)
        self.assertFalse(hook_retriever.config.llm_rerank)
        self.assertTrue(self.config.llm_rerank)

    def test_processes_queue_and_keeps_automatic_memories_pending(self):
        queue = SessionQueue(self.config)
        queue.enqueue(
            payload={
                "transcript": "The user explicitly decided: keep the worker local.",
                "session_id": "session-3",
                "summary": "Worker queue implementation",
                "next_steps": ["Review pending memories"],
            },
            session_id="session-3",
        )
        saved_item = MemoryItem(id="queued-memory", content="Keep the worker local.")

        with mock.patch("agent_m3m.cli.MemoryExtractor") as extractor_class:
            extractor_class.return_value.extract_from_transcript.return_value = [saved_item]
            report = _process_session_queue(self.config, self.store, self.retriever)

        self.assertEqual(report.processed, 1)
        self.assertEqual(report.failed, 0)
        call = extractor_class.return_value.extract_from_transcript.call_args
        self.assertTrue(call.kwargs["require_approval"])
        self.assertEqual(call.kwargs["source"], "session-end")
        self.assertEqual(SessionQueue(self.config).counts(), {"pending": 0, "processing": 0, "failed": 0, "done": 1})
        handoff = HandoffStore(self.config).load()
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.session_id, "session-3")

    def test_failure_keeps_payload_and_retry_is_idempotent(self):
        queue = SessionQueue(self.config)
        entry = queue.enqueue(payload={"transcript": "retry me"}, session_id="session-4")

        first = queue.process(lambda _entry: (_ for _ in ()).throw(RuntimeError("temporary provider failure")))
        self.assertEqual(first.failed, 1)
        self.assertEqual(queue.counts(), {"pending": 0, "processing": 0, "failed": 1, "done": 0})
        failed_entry = queue.list_entries("failed")[0]
        self.assertEqual(failed_entry.job_id, entry.job_id)
        self.assertEqual(failed_entry.payload["transcript"], "retry me")
        self.assertEqual(failed_entry.attempts, 1)

        second = queue.process(lambda queued: {"job_id": queued.job_id}, retry_failed=True)
        self.assertEqual(second.retried, 1)
        self.assertEqual(second.processed, 1)
        self.assertEqual(second.failed, 0)
        self.assertEqual(queue.counts(), {"pending": 0, "processing": 0, "failed": 0, "done": 1})
        self.assertEqual(queue.process(lambda _entry: None).processed, 0)

    def test_handoff_requires_confirmation_and_consumption_is_idempotent(self):
        handoffs = HandoffStore(self.config, ttl_seconds=3600)
        handoffs.save(Handoff(session_id="session-5", summary="Continue later"))
        current = handoffs.load()
        self.assertIsNotNone(current)

        self.assertFalse(handoffs.consume(current.handoff_id, confirmed=False))
        self.assertIsNotNone(handoffs.load())
        self.assertTrue(handoffs.consume(current.handoff_id, confirmed=True))
        self.assertTrue(handoffs.consume(current.handoff_id, confirmed=False))
        self.assertIsNone(handoffs.load())
        consumed = handoffs.load(include_consumed=True)
        self.assertIsNotNone(consumed)
        self.assertIsNotNone(consumed.consumed_at)

    def test_expired_handoff_is_not_loaded(self):
        old = datetime.now(timezone.utc) - timedelta(days=2)
        expires = (old + timedelta(seconds=1)).isoformat()
        handoffs = HandoffStore(self.config, ttl_seconds=1)
        handoffs.save(Handoff(session_id="expired", created_at=old.isoformat(), expires_at=expires))

        self.assertIsNone(handoffs.load())
        self.assertIsNotNone(handoffs.load(include_expired=True))


if __name__ == "__main__":
    unittest.main()
