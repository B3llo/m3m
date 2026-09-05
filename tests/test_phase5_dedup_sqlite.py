"""Phase 5 tests: duplicate prevention and legacy SQLite imports."""

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.importer import MemoryImporter
from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus, MemoryType
from agent_m3m.store import MemoryStore


class DedupTestBase(unittest.TestCase):
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

    def _cli(self, *argv):
        import sys
        output = io.StringIO()
        with mock.patch("agent_m3m.cli.Config", return_value=self.config):
            from agent_m3m.cli import main
            with mock.patch.object(sys, "argv", ["m3m", *argv]):
                with redirect_stdout(output):
                    main()
        return output.getvalue()


class TestImportFuzzyDedup(DedupTestBase):
    def test_import_skips_paraphrase_of_existing_memory(self):
        self.store.save_item(MemoryItem(
            id="mem_orig",
            content="O m3m usa um plugin ESM do OpenCode como harness de memória para sessões.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
        ))
        legacy = self.temp_dir_path() / "notes.md"
        legacy.write_text(
            "- O m3m usa um plugin ESM do OpenCode como harness de memória para as sessões.\n",
            encoding="utf-8",
        )

        imported = MemoryImporter(self.store, self.config).import_from_source(legacy)

        self.assertEqual(imported, [])
        self.assertEqual(self.store.stats()["total"], 1)

    def test_import_collapses_paraphrases_within_the_same_batch(self):
        legacy = self.temp_dir_path() / "notes.md"
        legacy.write_text(
            "- Sempre rode pytest -m unit antes de commitar código novo.\n"
            "- Sempre rode pytest -m unit antes de commitar o código novo.\n",
            encoding="utf-8",
        )

        imported = MemoryImporter(self.store, self.config).import_from_source(legacy)

        self.assertEqual(len(imported), 1)

    def test_import_keeps_genuinely_different_memories(self):
        legacy = self.temp_dir_path() / "notes.md"
        legacy.write_text(
            "- Sempre rode pytest -m unit antes de commitar.\n"
            "- Deploy de produção exige tag git assinada no repositório.\n",
            encoding="utf-8",
        )

        imported = MemoryImporter(self.store, self.config).import_from_source(legacy)

        self.assertEqual(len(imported), 2)

    def temp_dir_path(self) -> Path:
        return Path(self.temp_dir.name)


class TestDedupCommand(DedupTestBase):
    def _save(self, **kwargs):
        defaults = dict(scope=MemoryScope.PROJECT, type=MemoryType.WORKFLOW, status=MemoryStatus.ACTIVE)
        defaults.update(kwargs)
        self.store.save_item(MemoryItem(**defaults))

    def test_dry_run_reports_without_changing_status(self):
        original = self._save(id="mem_orig", content="Não executar o deploy sem aprovação explícita do usuário.", use_count=5)
        self._save(id="mem_dup", content="Não executar o deploy sem a aprovação explícita do usuário.")

        output = self._cli("dedup")

        self.assertIn("would supersede 1", output)
        self.assertIn("keep mem_orig", output)
        self.assertEqual(self.store.load_item_by_id("mem_dup").status, MemoryStatus.ACTIVE)

    def test_apply_supersedes_weaker_variant_and_keeps_most_used(self):
        self._save(
            id="mem_unused",
            content="Não executar o deploy sem aprovação explícita do usuário; validação e planejamento local não autorizam a aplicação.",
            use_count=9,
        )
        self._save(
            id="mem_used",
            content="Nunca executar o deploy sem aprovação explícita do usuário; validação e planejamento local não autorizam aplicação.",
            use_count=2,
        )

        output = self._cli("dedup", "--apply")

        self.assertIn("superseded 1", output)
        keeper = self.store.load_item_by_id("mem_unused")
        duplicate = self.store.load_item_by_id("mem_used")
        self.assertEqual(keeper.status, MemoryStatus.ACTIVE)
        self.assertEqual(duplicate.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(duplicate.superseded_by, "mem_unused")
        self.assertIn("dedup", duplicate.tags)
        # Restorable history.
        self.assertEqual(self.store.list_items(status=MemoryStatus.SUPERSEDED)[0].id, "mem_used")

    def test_pinned_memories_are_never_removed(self):
        self._save(id="mem_pinned", content="Deploy usa tag git assinada sempre.", pinned=True)
        self._save(id="mem_dup", content="Deploy sempre usa tag git assinada.", use_count=3)

        self._cli("dedup", "--apply")

        self.assertEqual(self.store.load_item_by_id("mem_pinned").status, MemoryStatus.ACTIVE)
        self.assertEqual(self.store.load_item_by_id("mem_dup").status, MemoryStatus.ACTIVE)

    def test_related_but_distinct_memories_survive(self):
        self._save(id="mem_a", content="Rodar pytest -m unit antes de commitar.")
        self._save(id="mem_b", content="Migrações SQLite rodam com o comando m3m clean.")

        output = self._cli("dedup")

        self.assertIn("no near-duplicates found", output)


class TestLegacySqliteImport(DedupTestBase):
    def test_imports_rows_from_sqlite_observations(self):
        db = self.temp_dir_path() / "memory.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE observations (id INTEGER PRIMARY KEY, content TEXT, project TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO observations (content, project, created_at) VALUES (?, ?, ?)",
            ("Fix: restart the worker service after each deploy.", "acme", "2026-08-28"),
        )
        conn.execute("INSERT INTO observations (content, project, created_at) VALUES (?, ?, ?)", ("", "acme", "2026-08-28"))
        conn.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
        conn.execute("INSERT INTO metadata VALUES ('schema', '1')")
        conn.commit()
        conn.close()

        imported = MemoryImporter(self.store, self.config).import_from_source(db)

        self.assertEqual(len(imported), 1)
        item = imported[0]
        self.assertIn("restart the worker", item.content)
        self.assertIn("legacy-sqlite", item.tags)
        self.assertIn("observations", item.tags)
        self.assertIn("acme", item.tags)
        self.assertEqual(item.scope, MemoryScope.GLOBAL)

    def test_scan_sources_finds_legacy_sqlite_store(self):
        fake_home = self.temp_dir_path() / "home"
        db_dir = fake_home / ".claude-mem" / "database"
        db_dir.mkdir(parents=True)
        (db_dir / "memory.db").write_bytes(b"placeholder")
        importer = MemoryImporter(self.store, self.config)
        importer.config.home_dir = fake_home

        labels = [label for label, _path, _count in importer.scan_sources()]

        self.assertTrue(any("Legacy SQLite memory store" in label for label in labels))

    def temp_dir_path(self) -> Path:
        return Path(self.temp_dir.name)


if __name__ == "__main__":
    unittest.main()
