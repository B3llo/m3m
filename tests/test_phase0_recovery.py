"""Recovery regressions for the final Phase 0 hardening pass."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_m3m.cli import (
    _approve_pending_memory,
    _enqueue_session_event,
    _process_session_queue_entry,
)
from agent_m3m.config import Config
from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus, MemoryType
from agent_m3m.queue import SessionQueue
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore


class TestPhase0Recovery(unittest.TestCase):
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

    def test_hook_policy_ignores_all_adulterable_origin_fields(self):
        entry = _enqueue_session_event(
            self.config,
            json.dumps({"transcript": "USER: retain this decision", "session_id": "session-1"}),
            {"transcript": "USER: retain this decision", "session_id": "session-1"},
        )
        path = self.config.project_dir / "sessions" / "queue" / "pending" / f"{entry.job_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source"] = "manual-save"
        data["require_approval"] = False
        data["captured_by_hook"] = False
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = SessionQueue(self.config).list_entries("pending")[0]
        self.assertTrue(loaded.job_id.startswith("hook_"))
        with mock.patch("agent_m3m.cli.MemoryExtractor") as extractor_class:
            extractor_class.return_value.extract_from_transcript.return_value = []
            _process_session_queue_entry(
                self.config,
                self.store,
                self.retriever,
                loaded,
            )

        call = extractor_class.return_value.extract_from_transcript.call_args
        self.assertEqual(call.kwargs["source"], "session-end")
        self.assertTrue(call.kwargs["require_approval"])

    def test_provider_failure_without_effects_is_retryable(self):
        queue = SessionQueue(self.config)
        entry = queue.enqueue(
            payload={"transcript": "provider retry"},
            captured_by_hook=True,
        )
        calls = []

        def processor(current):
            calls.append(current.job_id)
            if len(calls) == 1:
                raise RuntimeError("temporary provider failure")
            return {"saved": 0}

        first = queue.process(processor)
        self.assertEqual(first.failed, 1)
        failed = queue.list_entries("failed")[0]
        self.assertFalse(failed.retry_blocked)
        self.assertEqual(failed.job_id, entry.job_id)

        second = queue.process(processor, retry_failed=True)
        self.assertEqual(second.retried, 1)
        self.assertEqual(second.processed, 1)
        self.assertEqual(queue.counts(), {"pending": 0, "processing": 0, "failed": 0, "done": 1})

    def test_requeue_write_failure_preserves_failed_file(self):
        queue = SessionQueue(self.config)
        entry = queue.enqueue(payload={"transcript": "keep failed"}, captured_by_hook=True)
        claimed = queue.claim_next()
        self.assertTrue(queue.fail(claimed.job_id, "provider unavailable", claimed.owner_token))

        failed_path = self.config.project_dir / "sessions" / "queue" / "failed" / f"{entry.job_id}.json"
        original = failed_path.read_text(encoding="utf-8")
        with mock.patch.object(queue, "_atomic_write", side_effect=OSError("requeue preparation failed")):
            self.assertEqual(queue.requeue_failed(entry.job_id), 0)

        self.assertTrue(failed_path.is_file())
        self.assertEqual(failed_path.read_text(encoding="utf-8"), original)
        self.assertFalse(
            (self.config.project_dir / "sessions" / "queue" / "pending" / f"{entry.job_id}.json").exists()
        )

    def test_double_failure_approval_restores_old_and_reconciles_new(self):
        old = MemoryItem(
            id="recovery-old",
            content="Use the old workflow.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            type=MemoryType.CONVENTION,
            status=MemoryStatus.ACTIVE,
        )
        pending = MemoryItem(
            id="recovery-new",
            content="Use the new workflow.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            type=MemoryType.CONVENTION,
            status=MemoryStatus.PENDING,
            superseded_by=old.id,
        )
        self.store.save_item(old)
        self.store.save_item(pending)
        original_save = self.store.save_item

        def fail_during_transition(item):
            if item.id == pending.id and item.status == MemoryStatus.ACTIVE:
                return original_save(item)
            if item.id == old.id:
                raise OSError("old supersession failed")
            if item.id == pending.id:
                raise OSError("pending rollback failed")
            return original_save(item)

        with mock.patch.object(self.store, "save_item", side_effect=fail_during_transition):
            approved, message = _approve_pending_memory(self.store, pending.id)

        self.assertFalse(approved)
        self.assertIn("reconciliation required", message)
        self.assertEqual(self.store.load_item_by_id(old.id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.store.load_item_by_id(pending.id).status, MemoryStatus.PENDING)
        self.assertFalse(
            any(item.status == MemoryStatus.ACTIVE for item in self.store.list_items() if item.id == pending.id)
        )
        journal_dir = self.config.project_dir / "sessions" / "approval-reconciliation"
        self.assertTrue((journal_dir / f"{pending.id}.json").is_file())


if __name__ == "__main__":
    unittest.main()
