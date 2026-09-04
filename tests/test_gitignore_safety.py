"""Regression tests for private local-artifact gitignore protections."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.store import (
    AGENTS_EXPORT_GITIGNORE_ENTRY,
    MEMORY_EXPORT_GITIGNORE_ENTRY,
    PROJECT_MEMORY_GITIGNORE_ENTRY,
    MemoryStore,
    ensure_local_artifacts_ignored,
)
from agent_m3m.universal_export import run_universal_export


class GitignoreSafetyTests(unittest.TestCase):
    def test_creates_missing_gitignore_with_only_requested_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            result = ensure_local_artifacts_ignored(
                root,
                (PROJECT_MEMORY_GITIGNORE_ENTRY, MEMORY_EXPORT_GITIGNORE_ENTRY),
            )

            self.assertEqual(result, root / ".gitignore")
            self.assertEqual(
                result.read_text(encoding="utf-8").splitlines(),
                [PROJECT_MEMORY_GITIGNORE_ENTRY, MEMORY_EXPORT_GITIGNORE_ENTRY],
            )

    def test_preserves_existing_bytes_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            gitignore = root / ".gitignore"
            original = b"# existing rules\r\nbuild/\r\n/AGENTS.md\r\n"
            gitignore.write_bytes(original)

            requested = (
                PROJECT_MEMORY_GITIGNORE_ENTRY,
                AGENTS_EXPORT_GITIGNORE_ENTRY,
                MEMORY_EXPORT_GITIGNORE_ENTRY,
            )
            ensure_local_artifacts_ignored(root, requested)
            first = gitignore.read_bytes()
            ensure_local_artifacts_ignored(root, requested)

            self.assertTrue(first.startswith(original))
            self.assertEqual(gitignore.read_bytes(), first)
            self.assertEqual(first.count(b"/AGENTS.md"), 1)
            self.assertEqual(first.count(b"/.agent-m3m/"), 1)
            self.assertEqual(first.count(b"/MEMORY.md"), 1)
            self.assertNotIn(b"\n", first.replace(b"\r\n", b""))

    def test_project_init_only_ignores_project_memory_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                global_path=root / "global",
                project_path=root / ".agent-m3m",
            )
            store = MemoryStore(config)

            store.init_project()
            after_init = (root / ".gitignore").read_bytes()
            store.init_project()

            self.assertEqual((root / ".gitignore").read_bytes(), after_init)
            self.assertEqual(after_init, b"/.agent-m3m/\n")

    def test_custom_project_store_does_not_add_an_unrelated_ignore_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                global_path=root / "global",
                project_path=root / "custom-memory-store",
            )

            MemoryStore(config).init_project()

            self.assertTrue((root / "custom-memory-store" / "CORE.md").exists())
            self.assertFalse((root / ".gitignore").exists())

    def test_global_init_never_creates_or_changes_home_gitignore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            home.mkdir()
            config = Config(
                global_path=home / ".agent-m3m",
                project_path=root / "project" / ".agent-m3m",
            )
            config.project_dir = None
            store = MemoryStore(config)

            store.init_global()
            self.assertFalse((home / ".gitignore").exists())

            original = b"# user-owned home rules\r\n.private\r\n"
            (home / ".gitignore").write_bytes(original)
            store.init_global()
            self.assertEqual((home / ".gitignore").read_bytes(), original)

    def test_export_without_explicit_project_root_does_not_touch_home_gitignore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            config = Config(
                global_path=home / ".agent-m3m",
                project_path=home / "unused-project" / ".agent-m3m",
            )
            config.project_dir = None
            store = MemoryStore(config)

            with mock.patch("agent_m3m.universal_export.Path.cwd", return_value=home):
                run_universal_export(config, store, ["agents"])

            self.assertTrue((home / "AGENTS.md").exists())
            self.assertFalse((home / ".gitignore").exists())

    def test_export_ignores_only_the_selected_managed_root_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                global_path=root / "global",
                project_path=root / ".agent-m3m",
            )
            store = MemoryStore(config)

            run_universal_export(config, store, ["agents"])
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8"), "/AGENTS.md\n")

            run_universal_export(config, store, ["agents"])
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8"), "/AGENTS.md\n")

            run_universal_export(config, store, ["memory"])
            self.assertEqual(
                (root / ".gitignore").read_text(encoding="utf-8"),
                "/AGENTS.md\n/MEMORY.md\n",
            )

    def test_non_root_export_does_not_create_gitignore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                global_path=root / "global",
                project_path=root / ".agent-m3m",
            )
            store = MemoryStore(config)

            run_universal_export(config, store, ["cursor"])

            self.assertTrue((root / ".cursor" / "rules" / "m3m-memory.mdc").exists())
            self.assertFalse((root / ".gitignore").exists())

    def test_dry_run_does_not_create_gitignore_or_exports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                global_path=root / "global",
                project_path=root / ".agent-m3m",
            )
            store = MemoryStore(config)

            run_universal_export(config, store, ["agents", "memory"], dry_run=True)

            self.assertFalse((root / ".gitignore").exists())
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "MEMORY.md").exists())

    def test_empty_or_invalid_export_does_not_create_gitignore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                global_path=root / "global",
                project_path=root / ".agent-m3m",
            )
            store = MemoryStore(config)

            self.assertEqual(run_universal_export(config, store, []), [])
            self.assertFalse((root / ".gitignore").exists())
            with self.assertRaises(KeyError):
                run_universal_export(config, store, ["unknown"])
            self.assertFalse((root / ".gitignore").exists())


if __name__ == "__main__":
    unittest.main()
