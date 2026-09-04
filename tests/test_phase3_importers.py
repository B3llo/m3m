"""Phase 3 tests: universal migration into m3m from real-world sources."""

import json
import tempfile
import unittest
from pathlib import Path

from agent_m3m.config import Config
from agent_m3m.importer import MemoryImporter
from agent_m3m.models import MemoryScope, MemoryType
from agent_m3m.store import MemoryStore


class TestUniversalImport(unittest.TestCase):
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
        self.importer = MemoryImporter(self.store, self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_prose_claude_md_with_sections_is_no_longer_dropped(self):
        claude_md = self.temp_dir_path() / "CLAUDE.md"
        filler = "Esta decisão arquitetural sustenta todo o fluxo de autenticação do serviço. "
        claude_md.write_text(
            "# Project Guide\n\n"
            "Intro paragraph that is not a bullet and carries no memory by itself.\n\n"
            "## Decisões\n\n"
            f"- A fila de sessões usa arquivos JSON locais.\n"
            f"Texto corrido: {filler * 30}\n\n"
            "## Lições\n\n"
            "- Nunca rode o build com cache frio no CI.\n",
            encoding="utf-8",
        )

        imported = self.importer.import_from_source(claude_md)

        contents = [item.content for item in imported]
        self.assertTrue(any("arquivos JSON locais" in content for content in contents))
        self.assertTrue(any("cache frio no CI" in content for content in contents))
        self.assertTrue(any("Texto corrido" in content for content in contents))
        types = {item.type for item in imported}
        self.assertIn(MemoryType.DECISION, types)
        self.assertIn(MemoryType.LESSON, types)

    def test_cursor_mdc_foreign_frontmatter_is_stripped_not_dropped(self):
        rules_dir = self.temp_dir_path() / ".cursor" / "rules"
        rules_dir.mkdir(parents=True)
        rule = rules_dir / "testing.mdc"
        rule.write_text(
            "---\ndescription: Test conventions\nglobs:\n  - \"tests/**\"\nalwaysApply: false\n---\n\n"
            "# Testing Rules\n\n"
            "- Always run pytest with the local database fixture.\n"
            "- Use factory-boy instead of raw fixtures.\n",
            encoding="utf-8",
        )

        imported = self.importer.import_from_source(rule)

        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0].scope, MemoryScope.PROJECT)
        self.assertIn("pytest", imported[0].content)

    def test_mem0_style_json_with_memory_key_imports(self):
        export = self.temp_dir_path() / "mem0-export.json"
        export.write_text(json.dumps([
            {"id": "1", "memory": "User prefers dark mode terminals.", "createdAt": "2026-01-01"},
            {"id": "2", "memory": "The deploy pipeline uses GitHub Actions.", "createdAt": "2026-01-02"},
        ]), encoding="utf-8")

        imported = self.importer.import_from_source(export)

        self.assertEqual(len(imported), 2)
        self.assertTrue(any("dark mode" in item.content for item in imported))

    def test_claude_code_auto_memory_type_mapping(self):
        # Claude memory lives under ~/.claude/projects/<project>/memory and is
        # classified as global scope, so emulate that layout under a fake home.
        fake_home = self.temp_dir_path() / "home"
        memory_dir = (
            fake_home / ".claude" / "projects" / "-Users-testuser-project" / "memory"
        )
        memory_dir.mkdir(parents=True)
        (memory_dir / "feedback_testing.md").write_text(
            "---\ntype: feedback\n---\n\n"
            "- When the user says quickly, they still want the full test run.\n",
            encoding="utf-8",
        )
        (memory_dir / "user_role.md").write_text(
            "---\ntype: user\n---\n\n"
            "- The user is a backend engineer who prefers Python.\n",
            encoding="utf-8",
        )
        self.importer.config.home_dir = fake_home

        imported = self.importer.import_from_source(memory_dir)

        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0].scope, MemoryScope.GLOBAL)
        types = {item.type for item in imported}
        self.assertIn(MemoryType.LESSON, types)
        self.assertIn(MemoryType.PREFERENCE, types)

    def test_claude_memory_routes_to_decoded_project_store(self):
        # 'My-App' contains a literal dash, the ambiguity the decoder must
        # resolve against the real filesystem.
        root = self.temp_dir_path()
        fake_home = root / "home"
        other_project = root / "My-App"
        other_project.mkdir(parents=True)
        encoded = "-" + str(other_project)[1:].replace("/", "-")
        memory_dir = fake_home / ".claude" / "projects" / encoded / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "project_context.md").write_text(
            "---\ntype: project\n---\n\n"
            "- The scheduler uses filesystem leases.\n",
            encoding="utf-8",
        )
        self.importer.config.home_dir = fake_home

        self.assertEqual(self.importer.describe_route(memory_dir), f"project {other_project.name}")

        imported = self.importer.import_from_source(memory_dir)

        self.assertEqual(len(imported), 1)
        # Saved into the decoded project's own store, not the current one.
        self.assertTrue((other_project / ".agent-m3m" / "project_facts").exists())
        self.assertEqual(self.store.stats()["total"], 0)

    def test_current_project_claude_memory_stays_in_this_project(self):
        fake_home = self.temp_dir_path() / "home"
        current_root = self.config.project_root
        encoded = "-" + str(current_root)[1:].replace("/", "-")
        memory_dir = fake_home / ".claude" / "projects" / encoded / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "project_context.md").write_text(
            "---\ntype: project\n---\n\n- Acme builds via the local Makefile.\n",
            encoding="utf-8",
        )
        self.importer.config.home_dir = fake_home

        self.assertEqual(self.importer.describe_route(memory_dir), "this project")

        imported = self.importer.import_from_source(memory_dir)

        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].scope, MemoryScope.PROJECT)
        self.assertEqual(imported[0].project_id, self.config.project_id)

    def test_unresolved_claude_memory_falls_back_to_global(self):
        fake_home = self.temp_dir_path() / "home"
        memory_dir = fake_home / ".claude" / "projects" / "-nonexistent-xyz" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "notes.md").write_text(
            "- Orphaned memory with no decodable home.\n", encoding="utf-8"
        )
        self.importer.config.home_dir = fake_home

        self.assertEqual(
            self.importer.describe_route(memory_dir),
            "global (path not found on disk)",
        )

        imported = self.importer.import_from_source(memory_dir)

        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].scope, MemoryScope.GLOBAL)

    def test_dry_run_reports_without_saving(self):
        legacy = self.temp_dir_path() / "rules.md"
        legacy.write_text("- Always use uv for Python tooling.\n- Never commit secrets.\n", encoding="utf-8")

        found = self.importer.import_from_source(legacy, dry_run=True)

        self.assertEqual(len(found), 2)
        self.assertEqual(self.store.stats()["total"], 0)

    def test_import_is_idempotent_across_runs(self):
        legacy = self.temp_dir_path() / "rules.md"
        legacy.write_text(
            "# Guide\n\n## Convenções\n\n- Always use uv for Python tooling.\n",
            encoding="utf-8",
        )
        first = self.importer.import_from_source(legacy)
        second = self.importer.import_from_source(legacy)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def temp_dir_path(self) -> Path:
        return Path(self.temp_dir.name)


if __name__ == "__main__":
    unittest.main()
