"""Hardening tests: provider subprocess timeouts and single-flight workers."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.llm_runner import LLMRunner
from agent_m3m.queue import SessionQueue


class _ProcessTreeTimeoutTest(unittest.TestCase):
    def _config(self, root: Path, timeout: int) -> Config:
        config = Config(global_path=root / "global", project_path=root / "project")
        config.provider_timeout_seconds = timeout
        return config

    def test_timeout_terminates_entire_provider_process_tree(self):
        """A hung provider must not leave any descendant behind as an orphan.

        The provider command spawns its own long-lived child (as codex does
        with MCP servers and helper processes). When the provider times out,
        both parent and child must be gone by the time _run_simple returns.
        """
        child_code = "import time; time.sleep(30)"
        parent_code = (
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            "time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = LLMRunner(self._config(Path(tmp_dir), timeout=1))
            success, detail = runner._run_simple([sys.executable, "-c", parent_code])

        self.assertFalse(success)
        self.assertIn("timed out", detail)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["pgrep", "-f", "time.sleep(30)"],
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                break
            time.sleep(0.1)
        probe = subprocess.run(
            ["pgrep", "-f", "time.sleep(30)"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            probe.returncode,
            0,
            "provider descendant survived the timeout kill",
        )

    def test_successful_short_command_still_works(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = LLMRunner(self._config(Path(tmp_dir), timeout=10))
            success, output = runner._run_simple(
                [sys.executable, "-c", "print('structured-ok')"]
            )
        self.assertTrue(success)
        self.assertEqual(output, "structured-ok")


class _SingleFlightWorkerLockTest(unittest.TestCase):
    def _queue(self, root: Path) -> SessionQueue:
        config = Config(global_path=root / "global", project_path=root / "project")
        return SessionQueue(config)

    def test_second_worker_is_blocked_while_first_holds_lock(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            queue = self._queue(Path(tmp_dir))
            with queue.worker_lock(blocking=False) as acquired:
                self.assertTrue(acquired)
                self.assertTrue(queue.worker_lock_active())
                with queue.worker_lock(blocking=False) as second:
                    self.assertFalse(second)
            self.assertFalse(queue.worker_lock_active())

    def test_lock_is_released_after_context_exit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            queue = self._queue(Path(tmp_dir))
            for _ in range(2):
                with queue.worker_lock(blocking=False) as acquired:
                    self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
