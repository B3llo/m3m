"""Adversarial regression tests for the final Phase 0 hardening pass."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_m3m.cli import (
    _approve_pending_memory,
    _enqueue_session_event,
    _process_session_queue_entry,
)
from agent_m3m.config import Config
from agent_m3m.extractor import MemoryExtractor
from agent_m3m.handoff import Handoff, HandoffStore
from agent_m3m.models import MemoryItem, MemoryStatus, MemoryScope, MemoryType
from agent_m3m.queue import SessionQueue
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore


class _Runner:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def run_prompt(self, _prompt, **_kwargs):
        self.calls += 1
        return True, self.response


class TestPhase0Hardening(unittest.TestCase):
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

    def test_source_and_source_session_are_scrubbed_before_persist(self):
        self.config.secret_scrubbing = False
        item = MemoryItem(
            id="metadata-secret",
            content="safe content",
            source="manual Authorization: Bearer abcdefghijklmnop",
            source_session="ghp_1234567890abcdef1234567890abcdef",
        )
        self.store.save_item(item)

        raw = Path(item.file_path).read_text(encoding="utf-8")
        self.assertNotIn("abcdefghijklmnop", raw)
        self.assertNotIn("ghp_1234567890abcdef1234567890abcdef", raw)

    def test_hook_captured_origin_and_approval_cannot_be_downgraded(self):
        entry = _enqueue_session_event(
            self.config,
            json.dumps({"transcript": "USER: Keep this pending.", "session_id": "hook-session"}),
            {"transcript": "USER: Keep this pending.", "session_id": "hook-session"},
        )
        path = self.config.project_dir / "sessions" / "queue" / "pending" / f"{entry.job_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source"] = "manual-save"
        data["require_approval"] = False
        path.write_text(json.dumps(data), encoding="utf-8")
        adulterated = SessionQueue(self.config).list_entries("pending")[0]

        with mock.patch("agent_m3m.cli.MemoryExtractor") as extractor_class:
            extractor_class.return_value.extract_from_transcript.return_value = []
            _process_session_queue_entry(self.config, self.store, self.retriever, adulterated)

        call = extractor_class.return_value.extract_from_transcript.call_args
        self.assertEqual(call.kwargs["source"], "session-end")
        self.assertTrue(call.kwargs["require_approval"])

    def test_two_workers_cannot_claim_same_job(self):
        queue_a = SessionQueue(self.config, lease_seconds=1)
        queue_b = SessionQueue(self.config, lease_seconds=1)
        entry = queue_a.enqueue(payload={"transcript": "one job"})
        barrier = threading.Barrier(3)
        claims = []

        def claim(queue):
            barrier.wait()
            claims.append(queue.claim_next())

        workers = [threading.Thread(target=claim, args=(queue,)) for queue in (queue_a, queue_b)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)

        claimed = [item for item in claims if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].job_id, entry.job_id)
        self.assertEqual(queue_a.counts()["processing"], 1)

    def test_stale_recovery_is_serialized_between_workers(self):
        initial_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = SessionQueue(self.config, lease_seconds=1)
        second = SessionQueue(self.config, lease_seconds=1)
        with mock.patch("agent_m3m.queue._utc_now", return_value=initial_time):
            entry = first.enqueue(payload={"transcript": "stale job"})
            claimed = first.claim_next()
            self.assertEqual(claimed.job_id, entry.job_id)

        barrier = threading.Barrier(3)
        claims = []

        def recover(queue):
            barrier.wait()
            claims.append(queue.claim_next())

        after_expiry = initial_time + timedelta(seconds=2)
        with mock.patch("agent_m3m.queue._utc_now", return_value=after_expiry):
            workers = [threading.Thread(target=recover, args=(queue,)) for queue in (first, second)]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(timeout=2)

        recovered = [item for item in claims if item is not None]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].job_id, entry.job_id)

    def test_heartbeat_cannot_resurrect_done_job(self):
        queue = SessionQueue(self.config, lease_seconds=0.1)
        entry = queue.enqueue(payload={"transcript": "finish once"})
        claimed = queue.claim_next()
        self.assertTrue(queue.complete(claimed.job_id, claimed.owner_token))

        stop = threading.Event()
        heartbeat = threading.Thread(target=queue._heartbeat, args=(claimed.job_id, claimed.owner_token, stop))
        heartbeat.start()
        time.sleep(0.04)
        stop.set()
        heartbeat.join(timeout=1)

        self.assertFalse(queue.renew(claimed.job_id, claimed.owner_token))
        self.assertEqual(queue.counts(), {"pending": 0, "processing": 0, "failed": 0, "done": 1})

    def test_transitions_require_the_current_owner_token(self):
        queue = SessionQueue(self.config, lease_seconds=1)
        entry = queue.enqueue(payload={"transcript": "owner protected"})
        claimed = queue.claim_next()
        self.assertFalse(queue.renew(claimed.job_id))
        self.assertFalse(queue.renew(claimed.job_id, "worker_wrong"))
        self.assertFalse(queue.complete(claimed.job_id, "worker_wrong"))
        self.assertFalse(queue.fail(claimed.job_id, "wrong owner", "worker_wrong"))
        self.assertTrue(queue.complete(claimed.job_id, claimed.owner_token))

    def test_incompatible_directory_state_is_quarantined(self):
        queue = SessionQueue(self.config)
        entry = queue.enqueue(payload={"transcript": "bad state"})
        source = self.config.project_dir / "sessions" / "queue" / "pending" / f"{entry.job_id}.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        data["status"] = "done"
        source.write_text(json.dumps(data), encoding="utf-8")

        self.assertEqual(queue.list_entries("pending"), [])
        quarantined = list((self.config.project_dir / "sessions" / "queue" / "quarantine").glob("*.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertIsNone(queue.claim_next())

    def test_effect_checkpoint_prevents_partial_effect_replay(self):
        queue = SessionQueue(self.config)
        entry = queue.enqueue(payload={"transcript": "partial effect"}, captured_by_hook=True)
        side_effects = []
        first_call = True

        def processor(current):
            nonlocal first_call
            if not current.effects.get("memory_committed"):
                side_effects.append(current.job_id)
                self.assertTrue(queue.record_effects(
                    current.job_id,
                    current.owner_token,
                    {"memory_ids": ["mem-once"], "memory_committed": True},
                ))
                if first_call:
                    first_call = False
                    raise RuntimeError("failed after durable side effect")
            return {"saved": 1, "effects": {"memory_ids": ["mem-once"]}}

        first = queue.process(processor)
        self.assertEqual(first.failed, 1)
        second = queue.process(processor, retry_failed=True)
        self.assertEqual(second.processed, 1)
        self.assertEqual(side_effects, [entry.job_id])

    def test_complete_failure_retries_from_checkpoint_without_reinvoking_processor(self):
        queue = SessionQueue(self.config)
        queue.enqueue(payload={"transcript": "checkpointed"}, captured_by_hook=True)
        calls = []
        original_complete = queue.complete

        def processor(entry):
            calls.append(entry.job_id)
            return {"effects": {"external_id": entry.job_id}}

        with mock.patch.object(queue, "complete", side_effect=[False, original_complete]):
            first = queue.process(processor)
            self.assertEqual(first.failed, 1)
            second = queue.process(processor, retry_failed=True)

        self.assertEqual(second.processed, 1)
        self.assertEqual(len(calls), 1)

    def test_approval_write_failure_leaves_old_memory_active(self):
        old = MemoryItem(
            id="approval-old",
            content="Use the old workflow.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            type=MemoryType.CONVENTION,
            status=MemoryStatus.ACTIVE,
        )
        pending = MemoryItem(
            id="approval-new",
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

        def fail_new(item):
            if item.id == pending.id:
                raise OSError("new save failed")
            return original_save(item)

        with mock.patch.object(self.store, "save_item", side_effect=fail_new):
            approved, message = _approve_pending_memory(self.store, pending.id)

        self.assertFalse(approved)
        self.assertIn("new save failed", message)
        self.assertEqual(self.store.load_item_by_id(old.id).status, MemoryStatus.ACTIVE)

    def test_memory_and_handoff_idempotency_keys_reuse_effects(self):
        response = json.dumps({
            "memories": [{
                "content": "Keep the local worker.",
                "scope": "project",
                "type": "decision",
            }]
        })
        runner = _Runner(response)
        extractor = MemoryExtractor(
            store=self.store,
            retriever=self.retriever,
            llm_runner=runner,
            config=self.config,
        )
        first = extractor.extract_from_transcript(
            "USER: Keep the local worker.", idempotency_key="job-once"
        )
        second = extractor.extract_from_transcript(
            "USER: Keep the local worker.", idempotency_key="job-once"
        )
        self.assertEqual([item.id for item in first], [item.id for item in second])
        self.assertEqual(runner.calls, 1)
        self.assertEqual(len(self.store.list_items(include_expired=True)), 1)

        handoffs = HandoffStore(self.config)
        first_path = handoffs.save(Handoff(session_id="s", summary="same"), idempotency_key="job-once")
        second_path = handoffs.save(Handoff(session_id="s", summary="same"), idempotency_key="job-once")
        self.assertEqual(first_path, second_path)
        self.assertEqual(len(list((self.config.project_dir / "sessions" / "handoffs").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
