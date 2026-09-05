"""Safe-hardening tests: eval PT-BR corpus, sanitizer additions, doctor checks,
expiry filters, bounded diagnostics, and context --no-record-usage.

All additions are additive or dry-run-by-default; these tests lock that in.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_m3m.cli import (
    _build_agents_md,
    _build_index_markdown,
    _prune_old_files,
    _rotate_log_if_large,
    _tail_lines,
)
from agent_m3m.config import Config
from agent_m3m.context import ContextBuilder
from agent_m3m.doctor import Doctor
from agent_m3m.hooks_installer import HooksInstaller
from agent_m3m.models import MemoryItem, MemoryScope
from agent_m3m.sanitizer import has_secrets, scrub_secrets
from agent_m3m.store import MemoryStore


def _temp_config(temp_dir: str) -> Config:
    root = Path(temp_dir)
    return Config(global_path=root / "g", project_path=root / "p" / ".agent-m3m")


class SanitizerAdditionsTests(unittest.TestCase):
    def test_new_token_families_are_redacted(self):
        positives = [
            "gho_1234567890123456789012345678901234",
            "ghs_1234567890123456789012345678901234",
            "glft-12345678901234567890abcdef",
            "xoxe-12345678901234567890abcdef",
            "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234567",
            "ya29.a0AfH6SMBx1234567890abcdefghij12345",
            "AccountKey=abcdef1234567890ABCDEF==",
            "rk_live_12345678901234567890",
            "whsec_abcdef1234567890abcdef",
            "SG.abcdefghij1234567890.ABCDEFGHIJ1234567890abcdef",
            "123456789:AAE1234567890abcdef1234567890abcde",
            "https://api.example.com/v1?api_key=secret123456&x=1",
        ]
        for secret in positives:
            with self.subTest(secret=secret[:20]):
                self.assertIn("REDACTED", scrub_secrets(secret))
                self.assertTrue(has_secrets(secret))

    def test_benign_portuguese_text_is_untouched(self):
        negatives = [
            "bearer of good news for everyone today",
            "use orbitpm as the package manager",
            "xoxo is not a token, just a hug",
            "qual o prazo de reembolso Pix no billing-api",
        ]
        for text in negatives:
            with self.subTest(text=text[:30]):
                self.assertNotIn("REDACTED", scrub_secrets(text))
                self.assertFalse(has_secrets(text))


class ExpiryFilterTests(unittest.TestCase):
    def test_agents_md_excludes_expired_but_index_flags_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            store.save_item(MemoryItem(
                id="mem_fresh", content="Fresh active memory for testing.",
                scope=MemoryScope.PROJECT, project_id=config.project_id,
            ))
            store.save_item(MemoryItem(
                id="mem_old", content="Expired memory must not leak.",
                scope=MemoryScope.PROJECT, project_id=config.project_id,
                expires_at=past,
            ))
            items = store.list_items(include_expired=True)
            self.assertNotIn("Expired memory", _build_agents_md(items))
            self.assertIn("Fresh active", _build_agents_md(items))
            self.assertIn("expired", _build_index_markdown("# T", items))

    def test_context_never_injects_expired(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            store.save_item(MemoryItem(
                id="mem_ctx_fresh", content="Fresh context memory marker.",
                scope=MemoryScope.PROJECT, project_id=config.project_id,
            ))
            store.save_item(MemoryItem(
                id="mem_ctx_old", content="Expired context memory marker.",
                scope=MemoryScope.PROJECT, project_id=config.project_id,
                expires_at=past,
            ))
            context = ContextBuilder(store, config=config).build_context(
                prompt="context memory", record_usage=False,
            )
            self.assertIn("Fresh context memory marker", context)
            self.assertNotIn("Expired context memory marker", context)


class BoundedDiagnosticsTests(unittest.TestCase):
    def test_tail_lines_reads_only_the_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "diag.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for index in range(2000):
                    handle.write(json.dumps({"i": index}) + "\n")
            tail = _tail_lines(path, 20)
            self.assertEqual(len(tail), 20)
            self.assertEqual(json.loads(tail[-1])["i"], 1999)
            self.assertEqual(json.loads(tail[0])["i"], 1980)

    def test_rotation_is_bounded_and_fail_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            big = Path(temp_dir) / "m3m_hooks.log"
            big.write_bytes(b"x" * (6 * 1024 * 1024))
            self.assertTrue(_rotate_log_if_large(big))
            self.assertTrue(big.with_name(big.name + ".1").is_file())
            small = Path(temp_dir) / "small.log"
            small.write_text("hi", encoding="utf-8")
            self.assertFalse(_rotate_log_if_large(small))
            self.assertFalse(_rotate_log_if_large(Path(temp_dir) / "missing.log"))

    def test_prune_only_touches_old_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "done"
            directory.mkdir()
            old = directory / "old.json"
            old.write_text("{}", encoding="utf-8")
            os.utime(old, (0, 0))
            new = directory / "new.json"
            new.write_text("{}", encoding="utf-8")
            (directory / "notes.txt").write_text("keep", encoding="utf-8")
            candidates, _ = _prune_old_files(directory, 30, dry_run=True)
            self.assertIn(old, candidates)
            self.assertNotIn(new, candidates)
            self.assertTrue((directory / "notes.txt").exists())


class NoRecordUsageTests(unittest.TestCase):
    def test_record_usage_false_leaves_files_alone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            item = store.save_item(MemoryItem(
                id="mem_norecord", content="No-record usage marker memory.",
                scope=MemoryScope.PROJECT, project_id=config.project_id,
            ))
            before = Path(item.file_path).stat().st_mtime
            ContextBuilder(store, config=config).build_context(
                prompt="no-record marker", record_usage=False,
            )
            self.assertEqual(before, Path(item.file_path).stat().st_mtime)


class DoctorHygieneTests(unittest.TestCase):
    def test_new_checks_pass_on_clean_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            config.save_config()
            results = Doctor(config).run_diagnostics()
            by_name = {name: ok for name, _, ok in results}
            self.assertIn("Config Permissions", by_name)
            self.assertIn("Log Hygiene", by_name)
            self.assertIn("Queue Hygiene", by_name)
            self.assertTrue(by_name["Config Permissions"])
            self.assertTrue(by_name["Queue Hygiene"])


class HookFlagTests(unittest.TestCase):
    def test_codex_user_prompt_submit_skips_usage_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self.assertTrue(HooksInstaller(home_dir=home).install_codex_hooks()[2])
            installed = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for entry in installed["hooks"]["UserPromptSubmit"]
                for hook in entry.get("hooks", [])
            ]
            self.assertEqual(
                commands,
                ["m3m context --hook-output --hook-event UserPromptSubmit --no-record-usage"],
            )
            # SessionStart keeps the reinforcement signal (once per session).
            start = [
                hook["command"]
                for entry in installed["hooks"]["SessionStart"]
                for hook in entry.get("hooks", [])
            ]
            self.assertIn("m3m context --hook-output", start)

    def test_opencode_template_matches_repo_harness_with_flag(self):
        root = Path(__file__).resolve().parent.parent
        repo_plugin = (root / "hooks" / "opencode_plugin.js").read_text(encoding="utf-8")
        self.assertIn("--no-record-usage", repo_plugin)
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            HooksInstaller(home_dir=home).install_opencode_hooks()
            installed = (home / ".config" / "opencode" / "plugins" / "m3m-memory.js").read_text(encoding="utf-8")
            self.assertEqual(repo_plugin, installed)


class EvalCorpusTests(unittest.TestCase):
    def test_pt_corpus_is_present_and_gated(self):
        root = Path(__file__).resolve().parent.parent
        payload = json.loads((root / "evals" / "retrieval_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("schema_version"), 1)
        ids = [case["id"] for case in payload["cases"]]
        self.assertGreaterEqual(len(ids), 20)
        for expected in ("pix-refund", "deploy-storefront", "pytest-unit", "main-gate"):
            self.assertIn(expected, ids)


if __name__ == "__main__":
    unittest.main()
