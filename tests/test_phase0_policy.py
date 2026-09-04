"""Phase 0 policy tests: auto-approval, batch approve, queue drain, doctor backlog."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.doctor import Doctor
from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus, MemoryType
from agent_m3m.queue import SessionQueue
from agent_m3m.store import MemoryStore


def _run_cli(argv, stdin_data=""):
    from agent_m3m.cli import main

    output = io.StringIO()
    with mock.patch.object(sys_module(), "argv", argv), \
         mock.patch.object(sys_module(), "stdin", io.StringIO(stdin_data)):
        with redirect_stdout(output):
            main()
    return output.getvalue()


def sys_module():
    import sys
    return sys


class PolicyTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = Config(
            global_path=root / "global_memory",
            project_path=root / "project_memory",
        )
        self.store = MemoryStore(self.config)
        self.store.init_global()
        self.store.init_project()

    def tearDown(self):
        self.temp_dir.cleanup()


class TestAutoApproveConfidence(PolicyTestBase):
    def test_config_roundtrip_and_clamping(self):
        self.config.auto_approve_confidence = 0.75
        self.config.save_config()
        reloaded = Config(global_path=self.config.global_dir, project_path=self.config.project_dir)
        self.assertAlmostEqual(reloaded.auto_approve_confidence, 0.75)

        self.config.auto_approve_confidence = 5.0
        self.config.load()
        self.assertLessEqual(self.config.auto_approve_confidence, 1.0)

    def test_extractor_defaults_to_0_9_threshold(self):
        from agent_m3m.extractor import MemoryExtractor

        extractor = MemoryExtractor(self.store, config=self.config)
        self.assertAlmostEqual(extractor.config.auto_approve_confidence, 0.9)


class TestApproveAll(PolicyTestBase):
    def _cli(self, *argv):
        with mock.patch("agent_m3m.cli.Config", return_value=self.config):
            return _run_cli(["m3m", "approve", *argv])

    def test_approve_all_activates_every_pending_memory(self):
        low = MemoryItem(
            id="mem_low", content="low confidence finding", scope=MemoryScope.GLOBAL,
            type=MemoryType.CONVENTION, status=MemoryStatus.PENDING, confidence=0.7,
        )
        high = MemoryItem(
            id="mem_high", content="high confidence finding", scope=MemoryScope.GLOBAL,
            type=MemoryType.CONVENTION, status=MemoryStatus.PENDING, confidence=0.95,
        )
        self.store.save_item(low)
        self.store.save_item(high)

        output = self._cli("--all")

        self.assertIn("Approved 2", output)
        self.assertEqual(self.store.load_item_by_id("mem_low").status, MemoryStatus.ACTIVE)
        self.assertEqual(self.store.load_item_by_id("mem_high").status, MemoryStatus.ACTIVE)

    def test_approve_all_min_confidence_filters(self):
        low = MemoryItem(
            id="mem_low", content="low confidence finding", scope=MemoryScope.GLOBAL,
            type=MemoryType.CONVENTION, status=MemoryStatus.PENDING, confidence=0.7,
        )
        high = MemoryItem(
            id="mem_high", content="high confidence finding", scope=MemoryScope.GLOBAL,
            type=MemoryType.CONVENTION, status=MemoryStatus.PENDING, confidence=0.95,
        )
        self.store.save_item(low)
        self.store.save_item(high)

        output = self._cli("--all", "--min-confidence", "0.9")

        self.assertIn("Approved 1", output)
        self.assertEqual(self.store.load_item_by_id("mem_low").status, MemoryStatus.PENDING)
        self.assertEqual(self.store.load_item_by_id("mem_high").status, MemoryStatus.ACTIVE)

    def test_approve_all_without_pending_reports_zero(self):
        output = self._cli("--all")
        self.assertIn("Approved 0", output)


class TestQueueWorkerSpawn(PolicyTestBase):
    def test_spawns_worker_only_when_pending_jobs_exist(self):
        from agent_m3m.cli import _spawn_queue_worker

        with mock.patch("agent_m3m.cli.subprocess.Popen") as popen:
            _spawn_queue_worker(self.config)
        popen.assert_not_called()

        queue = SessionQueue(self.config)
        queue.enqueue(raw_input="USER: Remember the deploy convention.", captured_by_hook=True)

        with mock.patch("agent_m3m.cli.subprocess.Popen") as popen:
            _spawn_queue_worker(self.config)
        self.assertEqual(popen.call_count, 1)
        command = popen.call_args.args[0]
        self.assertIn("queue", command)
        self.assertIn("process", command)
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))

    def test_context_command_triggers_worker(self):
        queue = SessionQueue(self.config)
        queue.enqueue(raw_input="USER: Remember the deploy convention.", captured_by_hook=True)

        with mock.patch("agent_m3m.cli.Config", return_value=self.config), \
             mock.patch("agent_m3m.cli.subprocess.Popen") as popen:
            _run_cli(["m3m", "context"])
        self.assertEqual(popen.call_count, 1)


class TestDoctorPendingBacklog(PolicyTestBase):
    def test_doctor_reports_pending_backlog(self):
        doctor = Doctor(self.config)

        clean = {name: ok for name, _, ok in doctor.run_diagnostics()}
        self.assertTrue(clean["Pending Backlog"])

        self.store.save_item(MemoryItem(
            id="mem_pending", content="awaiting review", scope=MemoryScope.GLOBAL,
            status=MemoryStatus.PENDING,
        ))
        results = doctor.run_diagnostics()
        backlog = next(message for name, message, _ in results if name == "Pending Backlog")
        ok = next(ok for name, _, ok in results if name == "Pending Backlog")
        self.assertFalse(ok)
        self.assertIn("1 pending memory", backlog)


if __name__ == "__main__":
    unittest.main()
