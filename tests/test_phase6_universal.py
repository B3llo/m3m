"""Phase 6 tests: universal agent-file export (AGENTS.md, MEMORY.md, tool rules)."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.importer import MemoryImporter
from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus, MemoryType
from agent_m3m.store import MemoryStore
from agent_m3m.universal_export import (
    MARKER_END,
    MARKER_START,
    merge_managed_block,
    render_instructions,
    run_universal_export,
)


class UniversalExportBase(unittest.TestCase):
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

    def _save(self, **kwargs):
        defaults = dict(scope=MemoryScope.PROJECT, type=MemoryType.CONVENTION, status=MemoryStatus.ACTIVE)
        defaults.update(kwargs)
        self.store.save_item(MemoryItem(**defaults))
        return defaults["id"]

    def _cli(self, *argv):
        import sys
        output = io.StringIO()
        with mock.patch("agent_m3m.cli.Config", return_value=self.config):
            from agent_m3m.cli import main
            with mock.patch.object(sys, "argv", ["m3m", *argv]):
                with redirect_stdout(output):
                    main()
        return output.getvalue()


class TestManagedBlock(UniversalExportBase):
    def test_merge_into_empty_file_creates_block(self):
        merged = merge_managed_block("", f"{MARKER_START}\nbody\n{MARKER_END}\n")
        self.assertTrue(merged.startswith(MARKER_START))

    def test_merge_preserves_user_content_when_appending(self):
        existing = "# My AGENTS.md\n\n- My own rule: keep tests fast.\n"
        block = f"{MARKER_START}\n# Project Memory\n\n## Conventions\n- generated\n{MARKER_END}\n"

        merged = merge_managed_block(existing, block)

        self.assertTrue(merged.startswith("# My AGENTS.md"))
        self.assertIn("My own rule: keep tests fast.", merged)
        self.assertIn("## Conventions", merged)
        self.assertEqual(merged.count(MARKER_START), 1)

    def test_merge_replaces_existing_block_only(self):
        existing = (
            "# My AGENTS.md\n\n- My own rule.\n\n"
            f"{MARKER_START}\n## Old\n- stale\n{MARKER_END}\n"
        )
        block = f"{MARKER_START}\n## New\n- fresh\n{MARKER_END}\n"

        merged = merge_managed_block(existing, block)

        self.assertIn("My own rule.", merged)
        self.assertIn("## New", merged)
        self.assertNotIn("stale", merged)
        self.assertEqual(merged.count(MARKER_START), 1)


class TestRender(UniversalExportBase):
    def test_sections_ordered_and_bullets_clean(self):
        items = [
            MemoryItem(id="mem_a", content="Deploy requires a signed tag.", type=MemoryType.WORKFLOW),
            MemoryItem(id="mem_b", content="Markdown is the source of truth.", type=MemoryType.DECISION),
        ]
        body, count = render_instructions(items, max_lines=50)

        self.assertEqual(count, 2)
        self.assertIn("## Decisions", body)
        self.assertIn("## Workflows & Commands", body)
        self.assertLess(body.index("## Decisions"), body.index("## Workflows & Commands"))
        self.assertNotIn("id=mem_", body)

    def test_line_budget_limits_included_memories(self):
        items = [
            MemoryItem(id=f"mem_{index}", content=f"Rule number {index} for the project.", type=MemoryType.CONVENTION)
            for index in range(30)
        ]
        body, count = render_instructions(items, max_lines=10)

        # Title + blank + section header + blank + as many bullets as fit.
        self.assertEqual(count, 6)
        self.assertLessEqual(len(body.splitlines()), 10)
        self.assertNotIn("Rule number 6", body)


class TestExportTargets(UniversalExportBase):
    def test_default_targets_create_agents_and_memory(self):
        self._save(id="mem_one", content="Always run pytest before committing.")

        report = run_universal_export(self.config, self.store, ["agents", "memory"])
        actions = {row["target"]: row["action"] for row in report}

        self.assertEqual(actions, {"agents": "create", "memory": "create"})
        agents = self.config.project_root / "AGENTS.md"
        memory = self.config.project_root / "MEMORY.md"
        self.assertTrue(agents.exists())
        self.assertTrue(memory.exists())
        agents_text = agents.read_text(encoding="utf-8")
        self.assertIn("Always run pytest", agents_text)
        self.assertIn(MARKER_START, agents_text)
        memory_text = memory.read_text(encoding="utf-8")
        self.assertIn("(id=mem_one)", memory_text)

    def test_second_run_updates_block_in_place(self):
        self._save(id="mem_one", content="First generation rule.")
        run_universal_export(self.config, self.store, ["agents"])
        self._save(id="mem_two", content="Second generation rule.")

        report = run_universal_export(self.config, self.store, ["agents"])

        self.assertEqual(report[0]["action"], "update")
        agents_text = (self.config.project_root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Second generation rule.", agents_text)
        self.assertEqual(agents_text.count(MARKER_START), 1)

    def test_dry_run_writes_nothing(self):
        self._save(id="mem_one", content="Dry run rule.")

        report = run_universal_export(self.config, self.store, ["agents"], dry_run=True)

        self.assertEqual(report[0]["action"], "create")
        self.assertFalse((self.config.project_root / "AGENTS.md").exists())

    def test_tool_rule_targets_get_frontmatter(self):
        self._save(id="mem_one", content="Rule for tool targets.")

        run_universal_export(self.config, self.store, ["cursor", "windsurf", "kiro"])

        mdc = (self.config.project_root / ".cursor/rules/m3m-memory.mdc").read_text(encoding="utf-8")
        self.assertTrue(mdc.startswith("---\ndescription:"))
        self.assertIn("alwaysApply: true", mdc)
        windsurf = (self.config.project_root / ".windsurf/rules/m3m-memory.md").read_text(encoding="utf-8")
        self.assertIn("trigger: always_on", windsurf)
        kiro = (self.config.project_root / ".kiro/steering/m3m-memory.md").read_text(encoding="utf-8")
        self.assertIn("inclusion: always", kiro)

    def test_project_only_by_default_global_leak_never_exports(self):
        # Global store carries unrelated project knowledge (e.g. migrated
        # memories from a defunct project): it must not leak into exports.
        self.store.save_item(MemoryItem(
            id="mem_global_leak",
            content="Defunct project reference: Rosie anomaly detection plus Jarbas.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.PROJECT_FACT,
        ))
        self.store.save_item(MemoryItem(
            id="mem_global_pref",
            content="Prefers orbitpm over npm for Node.js projects.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.PREFERENCE,
        ))
        self._save(id="mem_proj", content="This project uses the filesystem queue.")

        run_universal_export(self.config, self.store, ["agents"])

        agents_text = (self.config.project_root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("This project uses the filesystem queue.", agents_text)
        self.assertNotIn("Defunct project reference", agents_text)
        self.assertNotIn("orbitpm over npm", agents_text)

    def test_include_global_flag_only_travels_preferences_and_conventions(self):
        self.store.save_item(MemoryItem(
            id="mem_global_pref", content="Prefers orbitpm over npm for Node.js projects.",
            scope=MemoryScope.GLOBAL, type=MemoryType.PREFERENCE,
        ))
        self.store.save_item(MemoryItem(
            id="mem_global_fact", content="Unrelated project fact that must stay home.",
            scope=MemoryScope.GLOBAL, type=MemoryType.PROJECT_FACT,
        ))
        self._save(id="mem_proj", content="Project-side rule.")

        run_universal_export(self.config, self.store, ["agents"], include_global=True)

        agents_text = (self.config.project_root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Prefers orbitpm over npm", agents_text)
        self.assertIn("Project-side rule.", agents_text)
        self.assertNotIn("Unrelated project fact", agents_text)

    def test_export_collapses_near_duplicate_variants(self):
        # The same fact captured across sessions in slightly different words
        # must appear once in the exported file, not five times.
        variants = [
            "For the API service, the compiled entrypoint is dist/src/main.js. Dockerfiles and production start scripts must invoke node dist/src/main.js.",
            "For the API service, the compiled entrypoint is dist/src/main.js. The Dockerfile and production start scripts must invoke node dist/src/main.js.",
            "For the API service, the compiled entrypoint is dist/src/main.js. Dockerfile and production start scripts must invoke node dist/src/main.js.",
            "For the API service, the compiled entrypoint is dist/src/main.js. The Dockerfile and production start script must invoke node dist/src/main.js, not /app/dist/main.js.",
            "For the API service, the compiled entrypoint is dist/src/main.js. The Dockerfile and production start script must invoke node dist/src/main.js, not /app/dist/main.js or dist/main.js.",
        ]
        for index, content in enumerate(variants):
            self._save(id=f"mem_var_{index}", content=content, type=MemoryType.LESSON)

        run_universal_export(self.config, self.store, ["agents"])

        agents_text = (self.config.project_root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_text.count("the compiled entrypoint is dist/src/main.js"), 1)

    def test_distinct_facts_survive_export_clustering(self):
        self._save(id="mem_deploy", content="Deploy uses immutable SHA image tags only.", type=MemoryType.DECISION)
        self._save(id="mem_build", content="Before deploying, run npm run build and verify dist/src/main.js exists.", type=MemoryType.WORKFLOW)

        run_universal_export(self.config, self.store, ["agents"])

        agents_text = (self.config.project_root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("immutable SHA image tags", agents_text)
        self.assertIn("npm run build", agents_text)

    def test_user_owned_agents_md_survives_export(self):
        agents = self.config.project_root / "AGENTS.md"
        agents.write_text("# Team conventions\n\n- Never force push to main.\n", encoding="utf-8")
        self._save(id="mem_one", content="Exported project rule.")

        run_universal_export(self.config, self.store, ["agents"])

        text = agents.read_text(encoding="utf-8")
        self.assertIn("# Team conventions", text)
        self.assertIn("Never force push to main.", text)
        self.assertIn("Exported project rule.", text)


class TestRoundtrip(UniversalExportBase):
    def test_reimporting_export_adds_nothing(self):
        self._save(id="mem_one", content="Roundtrip convention for the project.")
        self._save(id="mem_two", content="Deploy uses scenario-specific release plans.", type=MemoryType.DECISION)

        run_universal_export(self.config, self.store, ["agents"])

        agents = self.config.project_root / "AGENTS.md"
        importer = MemoryImporter(self.store, self.config)
        imported = importer.import_from_source(agents)

        self.assertEqual(imported, [])

    def test_manual_edits_outside_block_still_import(self):
        agents = self.config.project_root / "AGENTS.md"
        agents.write_text("# Manual note\n\n- Hand-written workflow: run make smoke.\n", encoding="utf-8")
        self._save(id="mem_one", content="Store-side rule.")
        run_universal_export(self.config, self.store, ["agents"])
        # User appends a hand-written section after the managed block.
        current = agents.read_text(encoding="utf-8")
        agents.write_text(current + "\n# Manual additions\n\n- Hand-written workflow: run make smoke.\n", encoding="utf-8")

        imported = MemoryImporter(self.store, self.config).import_from_source(agents)

        contents = " ".join(item.content for item in imported)
        self.assertIn("make smoke", contents)
        self.assertNotIn("Store-side rule.", contents)


if __name__ == "__main__":
    unittest.main()
