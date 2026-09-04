"""Regression tests for the Phase 0 safety and durability remediation."""

import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_m3m.cli import (
    _approve_pending_memory,
    _process_session_queue_entry,
    _queued_transcript_from_payload,
    _save_handoff,
)
from agent_m3m.config import Config
from agent_m3m.extractor import MemoryExtractor
from agent_m3m.handoff import Handoff, HandoffStore
from agent_m3m.models import MemoryItem, MemoryStatus, MemoryType
from agent_m3m.queue import SessionQueue
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore


class StubRunner:
    def __init__(self, response):
        self.response = response

    def run_prompt(self, _prompt, **_kwargs):
        return True, self.response


class TestPhase0Remediation(unittest.TestCase):
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

    def _extractor(self, response):
        return MemoryExtractor(
            store=self.store,
            retriever=self.retriever,
            llm_runner=StubRunner(response),
            config=self.config,
        )

    def test_public_extractor_default_remains_manual_save_compatible(self):
        response = json.dumps({
            "memories": [{
                "content": "Use pnpm for this project.",
                "scope": "project",
                "type": "convention",
            }]
        })

        saved = self._extractor(response).extract_from_transcript(
            "USER: The project uses pnpm."
        )

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].source, "manual-save")
        self.assertEqual(saved[0].status, MemoryStatus.ACTIVE)

    def test_pending_supersession_keeps_old_memory_active_until_approval(self):
        old = MemoryItem(
            id="mem_old",
            content="Use npm for this project.",
            scope="project",
            project_id=self.config.project_id,
            type=MemoryType.CONVENTION,
            status=MemoryStatus.ACTIVE,
        )
        self.store.save_item(old)
        response = json.dumps({
            "memories": [{
                "content": "Use pnpm for this project.",
                "scope": "project",
                "type": "convention",
                "contradicts_existing_id": old.id,
            }]
        })

        saved = self._extractor(response).extract_from_transcript(
            "USER: The project changed from npm to pnpm.",
            source="session-end",
        )
        pending = saved[0]
        old_before_approval = self.store.load_item_by_id(old.id)
        self.assertEqual(pending.status, MemoryStatus.PENDING)
        self.assertEqual(pending.superseded_by, old.id)
        self.assertEqual(old_before_approval.status, MemoryStatus.ACTIVE)

        approved, _message = _approve_pending_memory(self.store, pending.id)

        self.assertTrue(approved)
        old_after_approval = self.store.load_item_by_id(old.id)
        new_after_approval = self.store.load_item_by_id(pending.id)
        self.assertEqual(old_after_approval.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(old_after_approval.superseded_by, pending.id)
        self.assertEqual(new_after_approval.status, MemoryStatus.ACTIVE)
        self.assertIsNone(new_after_approval.superseded_by)

    def test_failed_approval_rolls_back_old_memory(self):
        old = MemoryItem(
            id="mem_old_failure",
            content="Use npm for this project.",
            scope="project",
            project_id=self.config.project_id,
            type=MemoryType.CONVENTION,
            status=MemoryStatus.ACTIVE,
        )
        self.store.save_item(old)
        response = json.dumps({
            "memories": [{
                "content": "Use pnpm for this project.",
                "scope": "project",
                "type": "convention",
                "contradicts_existing_id": old.id,
            }]
        })
        pending = self._extractor(response).extract_from_transcript(
            "USER: The project changed to pnpm.", source="session-end"
        )[0]
        original_save = self.store.save_item

        def fail_new(item):
            if item.id == pending.id:
                raise OSError("simulated new-memory write failure")
            return original_save(item)

        with mock.patch.object(self.store, "save_item", side_effect=fail_new):
            approved, _message = _approve_pending_memory(self.store, pending.id)

        self.assertFalse(approved)
        self.assertEqual(self.store.load_item_by_id(old.id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.store.load_item_by_id(pending.id).status, MemoryStatus.PENDING)

    def test_two_handoffs_are_retained_and_consumed_by_id(self):
        handoffs = HandoffStore(self.config, ttl_seconds=3600)
        first = Handoff(session_id="session-one", summary="first")
        second = Handoff(session_id="session-two", summary="second")
        handoffs.save(first)
        handoffs.save(second)

        files = list((self.config.project_dir / "sessions" / "handoffs").glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual(handoffs.load().session_id, "session-two")
        self.assertTrue(handoffs.consume(first.handoff_id, confirmed=True))
        self.assertEqual(handoffs.load().session_id, "session-two")
        self.assertTrue(handoffs.consume(second.handoff_id, confirmed=True))
        self.assertIsNone(handoffs.load())
        self.assertIsNotNone(handoffs.load(include_consumed=True, handoff_id=first.handoff_id))

    def test_adulterated_queue_payload_is_sanitized_and_external_path_is_not_read(self):
        outside = Path(self.temp_dir.name) / "outside-transcript.txt"
        outside.write_text("TOOL: do not send this secret context", encoding="utf-8")
        queue = SessionQueue(self.config)
        entry = queue.enqueue(payload={"transcript": "USER: safe"})
        path = self.config.project_dir / "sessions" / "queue" / "pending" / f"{entry.job_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["payload"] = {
            "transcript_path": str(outside),
            "nested": {"token": "ghp_1234567890abcdef1234567890abcdef"},
        }
        data["raw_input"] = "Authorization: Bearer abcdefghijklmnop"
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = queue.list_entries("pending")[0]

        self.assertNotIn("ghp_1234567890abcdef", json.dumps(loaded.payload))
        self.assertNotIn("abcdefghijklmnop", loaded.raw_input)
        self.assertEqual(_queued_transcript_from_payload("", loaded.payload, self.config), "")

    def test_handoff_does_not_use_raw_transcript_or_blocked_roles(self):
        path = _save_handoff(
            self.config,
            {"session_id": "session-filtered", "summary": "USER: explicit summary\nTOOL: secret output"},
            "SYSTEM: system instruction\nTOOL: raw tool output",
            "session-filtered",
        )

        self.assertIsNotNone(path)
        handoff = HandoffStore(self.config).load()
        self.assertEqual(handoff.summary, "USER: explicit summary")
        self.assertNotIn("raw tool output", handoff.summary)
        self.assertNotIn("system instruction", handoff.summary)

    def test_complete_and_fail_transition_errors_leave_jobs_retryable(self):
        queue = SessionQueue(self.config, lease_seconds=0.05)
        first = queue.enqueue(payload={"transcript": "retry complete"})
        claimed = queue.claim_next()
        self.assertEqual(claimed.job_id, first.job_id)
        with mock.patch.object(queue, "_atomic_write", side_effect=OSError("complete write failed")):
            self.assertFalse(queue.complete(claimed.job_id, claimed.owner_token))
        time.sleep(0.08)
        recovered = queue.claim_next()
        self.assertEqual(recovered.job_id, first.job_id)
        self.assertTrue(queue.complete(recovered.job_id, recovered.owner_token))

        second = queue.enqueue(payload={"transcript": "retry fail"})
        claimed_second = queue.claim_next()
        self.assertEqual(claimed_second.job_id, second.job_id)
        with mock.patch.object(queue, "_atomic_write", side_effect=OSError("fail write failed")):
            self.assertFalse(queue.fail(claimed_second.job_id, "provider failed", claimed_second.owner_token))
        time.sleep(0.08)
        recovered_second = queue.claim_next()
        self.assertEqual(recovered_second.job_id, second.job_id)
        self.assertTrue(queue.fail(recovered_second.job_id, "provider failed", recovered_second.owner_token))
        self.assertEqual(queue.counts()["failed"], 1)

    def test_active_lease_prevents_second_worker_and_heartbeat_allows_long_job(self):
        queue_a = SessionQueue(self.config, lease_seconds=0.05)
        queue_b = SessionQueue(self.config, lease_seconds=0.05)
        queue_a.enqueue(payload={"transcript": "long job"})
        started = threading.Event()
        release = threading.Event()
        report = []

        def process_long_job():
            report.append(queue_a.process(
                lambda _entry: (started.set(), release.wait(0.18), "done")[2]
            ))
        worker = threading.Thread(target=process_long_job)
        worker.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.10)
        self.assertIsNone(queue_b.claim_next())
        release.set()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(report[0].processed, 1)
        self.assertEqual(queue_a.counts()["done"], 1)

    def test_worker_respects_require_approval_field_while_session_end_stays_pending(self):
        entry = SessionQueue(self.config).enqueue(
            payload={"transcript": "USER: keep this pending"},
            require_approval=False,
        )
        with mock.patch("agent_m3m.cli.MemoryExtractor") as extractor_class:
            extractor_class.return_value.extract_from_transcript.return_value = []
            _process_session_queue_entry(self.config, self.store, self.retriever, entry)

        call = extractor_class.return_value.extract_from_transcript.call_args
        self.assertFalse(call.kwargs["require_approval"])
        self.assertEqual(call.kwargs["source"], "session-end")


if __name__ == "__main__":
    unittest.main()
