"""Tests for local memory-save diagnostics."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_m3m import cli
from agent_m3m.config import Config
from agent_m3m.store import MemoryStore


class TestMemoryDiagnostics(unittest.TestCase):
    def test_writes_scrubbed_structured_event_and_reads_it_back(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(Path, "home", return_value=Path(tmp_dir)):
            cli.log_memory_save_diagnostic({
                "outcome": "fallback",
                "reason": "provider_failure",
                "provider_error": "Authorization: Bearer abcdefghijklmnop",
                "saved_count": 0,
            })
            records = cli._read_memory_save_diagnostics(10)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "fallback")
        self.assertNotIn("abcdefghijklmnop", records[0]["provider_error"])
        self.assertIn("[REDACTED_BEARER_TOKEN]", records[0]["provider_error"])

    def test_empty_and_malformed_stop_payloads_are_structured_and_protocol_safe(self):
        cases = (("", "empty_hook_payload"), ("{", "malformed_hook_payload"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = Config(global_path=root / "global", project_path=root / "project")
            store = MemoryStore(config)
            store.init_global()
            store.init_project()

            for raw_input, expected_reason in cases:
                with self.subTest(expected_reason=expected_reason), \
                     mock.patch.object(Path, "home", return_value=root / "home"), \
                     mock.patch("agent_m3m.cli.Config", return_value=config), \
                     mock.patch("agent_m3m.cli.MemoryExtractor") as extractor_class, \
                     mock.patch.object(sys, "argv", ["m3m", "save", "--stdin", "--hook-output"]), \
                     mock.patch.object(sys, "stdin", io.StringIO(raw_input)):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        cli.main()
                    records = cli._read_memory_save_diagnostics(10)

                extractor_class.assert_not_called()
                self.assertEqual(
                    output.getvalue().strip(),
                    json.dumps({"continue": True, "suppressOutput": True}),
                )
                self.assertEqual(records[-1]["outcome"], "skipped")
                self.assertEqual(records[-1]["stage"], "input")
                self.assertEqual(records[-1]["reason"], expected_reason)
                if expected_reason == "malformed_hook_payload":
                    self.assertTrue(records[-1]["error"])

    def test_ordinary_json_envelope_is_not_sent_to_extractor(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = Config(global_path=root / "global", project_path=root / "project")
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            raw_input = json.dumps({"hook_event_name": "Stop", "session_id": "ordinary-envelope"})

            with mock.patch.object(Path, "home", return_value=root / "home"), \
                 mock.patch("agent_m3m.cli.Config", return_value=config), \
                 mock.patch("agent_m3m.cli.MemoryExtractor") as extractor_class, \
                 mock.patch.object(sys, "argv", ["m3m", "save", "--stdin", "--hook-output"]), \
                 mock.patch.object(sys, "stdin", io.StringIO(raw_input)):
                output = io.StringIO()
                with redirect_stdout(output):
                    cli.main()
                record = cli._read_memory_save_diagnostics(1)[0]

            extractor_class.assert_not_called()
            self.assertEqual(
                output.getvalue().strip(),
                json.dumps({"continue": True, "suppressOutput": True}),
            )
            self.assertEqual(record["reason"], "invalid_hook_payload")

    def test_audit_cli_command_displays_records(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with mock.patch.object(Path, "home", return_value=root):
                cli.log_memory_save_diagnostic({
                    "outcome": "success",
                    "source": "manual-save",
                    "saved_count": 1,
                    "memories": [{"id": "mem_123", "type": "workflow", "scope": "project", "content": "pytest -m unit"}],
                })
                output = io.StringIO()
                with mock.patch.object(sys, "argv", ["m3m", "audit"]):
                    with redirect_stdout(output):
                        cli.main()
                text = output.getvalue()
                self.assertIn("Memory Audit Log", text)
                self.assertIn("mem_123", text)
                self.assertIn("pytest -m unit", text)
