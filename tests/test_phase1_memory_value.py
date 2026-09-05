"""Phase 1 tests: usage feedback, pinning, consolidation and reconciliation."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.context import ContextBuilder
from agent_m3m.extractor import MemoryExtractor
from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus, MemoryType
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore


class ScriptedRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def run_prompt(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return True, self.responses.pop(0)


class Phase1TestBase(unittest.TestCase):
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
        self.retriever = MemoryRetriever(self.store, self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_extractor(self, runner):
        return MemoryExtractor(
            store=self.store,
            retriever=self.retriever,
            llm_runner=runner,
            config=self.config,
        )

    @staticmethod
    def memories_response(content, confidence=1.0):
        return json.dumps({
            "memories": [{
                "content": content,
                "scope": "project",
                "type": "convention",
                "confidence": confidence,
            }]
        })


class TestUsageFeedback(Phase1TestBase):
    def test_record_usage_bumps_counters_with_interval_guard(self):
        self.store.save_item(MemoryItem(id="mem_use", content="Use the deploy checklist.", scope=MemoryScope.GLOBAL))
        now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)

        self.assertEqual(self.store.record_usage(["mem_use"], now=now), 1)
        loaded = self.store.load_item_by_id("mem_use")
        self.assertEqual(loaded.use_count, 1)
        self.assertIsNotNone(loaded.last_used)
        original_updated = loaded.updated

        # Inside the write interval: no extra write.
        self.assertEqual(self.store.record_usage(["mem_use"], now=now + timedelta(hours=1)), 0)
        # After the interval: counted again, `updated` stays untouched.
        self.assertEqual(self.store.record_usage(["mem_use"], now=now + timedelta(hours=21)), 1)
        final = self.store.load_item_by_id("mem_use")
        self.assertEqual(final.use_count, 2)
        self.assertEqual(final.updated, original_updated)

    def test_use_bonus_breaks_lexical_ties(self):
        used = MemoryItem(
            id="mem_used",
            content="StackSpec deploys use scenario-specific plans with locked state.",
            scope=MemoryScope.GLOBAL,
            use_count=50,
        )
        fresh = MemoryItem(
            id="mem_fresh",
            content="StackSpec deploys prefer scenario-specific plans and locked state files.",
            scope=MemoryScope.GLOBAL,
        )
        self.store.save_item(used)
        self.store.save_item(fresh)
        results = self.retriever.search("stackspec deploys scenario-specific plans", top_k=2)
        self.assertEqual(results[0].item.id, "mem_used")
        self.assertGreater(results[0].breakdown["use_bonus"], 0.0)

    def test_context_records_usage_of_injected_items(self):
        self.store.save_item(MemoryItem(
            id="mem_injected",
            content="SQLite migrations run through the local workflow.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
        ))
        ContextBuilder(self.store, self.retriever, self.config).build_context(
            prompt="SQLite migrations workflow"
        )
        self.assertEqual(self.store.load_item_by_id("mem_injected").use_count, 1)


class TestPin(Phase1TestBase):
    def _cli(self, *argv):
        with mock.patch("agent_m3m.cli.Config", return_value=self.config):
            output = io_string()
            from agent_m3m.cli import main
            with mock.patch.object(sys_mod(), "argv", ["m3m", *argv]):
                from contextlib import redirect_stdout
                with redirect_stdout(output):
                    main()
            return output.getvalue()

    def test_pin_always_injects_into_context(self):
        self.store.save_item(MemoryItem(
            id="mem_pin",
            content="Always run the smoke suite before deploying.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
        ))
        self._cli("pin", "mem_pin")
        self.assertTrue(self.store.load_item_by_id("mem_pin").pinned)

        context = ContextBuilder(self.store, self.retriever, self.config).build_context()
        self.assertIn("### Pinned Memories", context)
        self.assertIn("Always run the smoke suite", context)

        self._cli("unpin", "mem_pin")
        context = ContextBuilder(self.store, self.retriever, self.config).build_context()
        self.assertNotIn("### Pinned Memories", context)


def io_string():
    import io
    return io.StringIO()


def sys_mod():
    import sys
    return sys


class TestConsolidate(Phase1TestBase):
    def _save(self, **kwargs):
        defaults = dict(scope=MemoryScope.PROJECT, type=MemoryType.DECISION, status=MemoryStatus.ACTIVE)
        defaults.update(kwargs)
        self.store.save_item(MemoryItem(**defaults))

    def test_consolidate_dry_run_reports_without_writing(self):
        self._save(id="mem_core", content="The chosen queue is a filesystem queue.", use_count=7)
        before = self.store.read_core_memory(MemoryScope.PROJECT)

        with mock.patch("agent_m3m.cli.Config", return_value=self.config):
            from contextlib import redirect_stdout
            import io
            output = io.StringIO()
            from agent_m3m.cli import main
            import sys
            with mock.patch.object(sys, "argv", ["m3m", "consolidate"]):
                with redirect_stdout(output):
                    main()

        self.assertIn("would include 1 memory", output.getvalue())
        self.assertEqual(self.store.read_core_memory(MemoryScope.PROJECT), before)

    def test_consolidate_apply_rewrites_core_and_reports_stale(self):
        self._save(id="mem_core", content="The chosen queue is a filesystem queue.", use_count=7)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        self._save(id="mem_stale", content="Ancient unused convention nobody recalls.")
        # save_item always refreshes `updated`, so age the file directly.
        item = self.store.load_item_by_id("mem_stale")
        item.updated = old_timestamp
        Path(item.file_path).write_text(item.to_markdown(), encoding="utf-8")

        from contextlib import redirect_stdout
        import io
        import sys
        output = io.StringIO()
        with mock.patch("agent_m3m.cli.Config", return_value=self.config):
            from agent_m3m.cli import main
            with mock.patch.object(sys, "argv", ["m3m", "consolidate", "--apply"]):
                with redirect_stdout(output):
                    main()

        core = self.store.read_core_memory(MemoryScope.PROJECT)
        self.assertIn("The chosen queue is a filesystem queue.", core)
        self.assertIn("(id=mem_core)", core)
        self.assertIn("never used in 90+ days", output.getvalue())
        self.assertIn("mem_stale", output.getvalue())
        # Consolidation is a curation layer: it never deletes or archives.
        self.assertEqual(self.store.load_item_by_id("mem_stale").status, MemoryStatus.ACTIVE)


class TestReconciliation(Phase1TestBase):
    def test_near_duplicate_is_skipped_without_reconcile_call(self):
        self.store.save_item(MemoryItem(
            id="mem_existing",
            content="Use orbitpm instead of npm for Node.js dependency management.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            status=MemoryStatus.ACTIVE,
        ))
        runner = ScriptedRunner([self.memories_response(
            "Use orbitpm instead of npm for Node.js dependency management tasks."
        )])
        extractor = self.make_extractor(runner)

        saved = extractor.extract_from_transcript(
            "USER: Use orbitpm instead of npm for Node.js dependency management tasks.",
            source="manual-save",
        )

        self.assertEqual(saved, [])
        # Only the extraction call ran; the near-duplicate skip is deterministic.
        self.assertEqual(len(runner.prompts), 1)
        self.assertEqual(extractor.last_diagnostic["reconciled"]["near_duplicate_skips"], 1)

    def test_confident_update_refines_existing_memory_in_place(self):
        self.store.save_item(MemoryItem(
            id="mem_target",
            content="Deploy with scenario-specific StackSpec plans.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            status=MemoryStatus.ACTIVE,
        ))
        candidate = "Deploy with scenario-specific StackSpec plans and versioned shared state across multiple environments."
        runner = ScriptedRunner([
            self.memories_response(candidate),
            json.dumps({"decisions": [{"candidate": 0, "action": "UPDATE", "id": "mem_target"}]}),
        ])
        extractor = self.make_extractor(runner)

        saved = extractor.extract_from_transcript(
            f"USER: {candidate}",
            source="manual-save",
        )

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].id, "mem_target")
        updated = self.store.load_item_by_id("mem_target")
        self.assertEqual(updated.content, candidate)
        self.assertEqual(updated.status, MemoryStatus.ACTIVE)
        self.assertEqual(extractor.last_diagnostic["reconciled"]["updates"], 1)

    def test_low_confidence_update_becomes_pending_supersede(self):
        self.store.save_item(MemoryItem(
            id="mem_target",
            content="Deploy with scenario-specific StackSpec plans.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            status=MemoryStatus.ACTIVE,
        ))
        candidate = "Deploy with scenario-specific StackSpec plans and versioned shared state across multiple environments."
        runner = ScriptedRunner([
            self.memories_response(candidate, confidence=0.7),
            json.dumps({"decisions": [{"candidate": 0, "action": "UPDATE", "id": "mem_target"}]}),
        ])
        extractor = self.make_extractor(runner)

        saved = extractor.extract_from_transcript(
            f"USER: {candidate}",
            source="manual-save",
        )

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].status, MemoryStatus.PENDING)
        self.assertEqual(saved[0].superseded_by, "mem_target")
        # The trusted memory stays active and untouched for human review.
        self.assertEqual(self.store.load_item_by_id("mem_target").status, MemoryStatus.ACTIVE)

    def test_supersede_action_keeps_old_active_until_approval(self):
        self.store.save_item(MemoryItem(
            id="mem_old",
            content="Deploy with scenario-specific StackSpec plans.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            status=MemoryStatus.ACTIVE,
        ))
        candidate = "Deploy with scenario-specific StackSpec plans and versioned shared state across multiple environments."
        runner = ScriptedRunner([
            self.memories_response(candidate),
            json.dumps({"decisions": [{"candidate": 0, "action": "SUPERSEDE", "id": "mem_old"}]}),
        ])
        extractor = self.make_extractor(runner)

        saved = extractor.extract_from_transcript(
            f"USER: The convention changed. {candidate}",
            source="manual-save",
        )

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].status, MemoryStatus.PENDING)
        self.assertEqual(saved[0].superseded_by, "mem_old")
        self.assertEqual(self.store.load_item_by_id("mem_old").status, MemoryStatus.ACTIVE)
        self.assertEqual(extractor.last_diagnostic["reconciled"]["supersedes"], 1)

    def test_failed_reconcile_call_fails_open_to_add(self):
        self.store.save_item(MemoryItem(
            id="mem_amb",
            content="Deploy with scenario-specific StackSpec plans.",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            status=MemoryStatus.ACTIVE,
        ))
        candidate = "Deploy with scenario-specific StackSpec plans and versioned shared state across multiple environments."

        class FailingReconcileRunner(ScriptedRunner):
            def run_prompt(self, prompt, **kwargs):
                if "reconcile" in prompt.lower():
                    return False, "provider down"
                return super().run_prompt(prompt, **kwargs)

        runner = FailingReconcileRunner([self.memories_response(candidate)])
        extractor = self.make_extractor(runner)

        saved = extractor.extract_from_transcript(f"USER: {candidate}", source="manual-save")

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].status, MemoryStatus.ACTIVE)


class TestRelationsAndIndex(Phase1TestBase):
    def _cli(self, *argv):
        from contextlib import redirect_stdout
        import io
        import sys
        output = io.StringIO()
        with mock.patch("agent_m3m.cli.Config", return_value=self.config):
            from agent_m3m.cli import main
            with mock.patch.object(sys, "argv", ["m3m", *argv]):
                with redirect_stdout(output):
                    main()
        return output.getvalue()

    def test_related_frontmatter_roundtrip(self):
        first = MemoryItem(id="mem_rel_a", content="First linked memory.", related=["mem_rel_b"])
        loaded = MemoryItem.from_markdown(first.to_markdown())
        self.assertEqual(loaded.related, ["mem_rel_b"])

    def test_link_is_bidirectional_and_related_resolves(self):
        self.store.save_item(MemoryItem(id="mem_rel_a", content="First linked memory."))
        self.store.save_item(MemoryItem(id="mem_rel_b", content="Second linked memory."))

        self._cli("link", "mem_rel_a", "mem_rel_b")

        self.assertIn("mem_rel_b", self.store.load_item_by_id("mem_rel_a").related)
        self.assertIn("mem_rel_a", self.store.load_item_by_id("mem_rel_b").related)

        output = self._cli("related", "mem_rel_a")
        self.assertIn("mem_rel_b", output)
        self.assertIn("Second linked memory.", output)

    def test_index_apply_writes_derived_overview(self):
        self.store.save_item(MemoryItem(
            id="mem_idx", content="Indexed decision.", pinned=True, use_count=3,
            scope=MemoryScope.PROJECT, project_id=self.config.project_id,
        ))
        self.store.save_item(MemoryItem(
            id="mem_pend", content="Pending candidate.", status=MemoryStatus.PENDING,
            scope=MemoryScope.PROJECT, project_id=self.config.project_id,
        ))

        output = self._cli("index", "--apply")

        self.assertIn("INDEX.md written", output)
        index_path = self.config.project_dir / "INDEX.md"
        self.assertTrue(index_path.exists())
        content = index_path.read_text(encoding="utf-8")
        self.assertIn("Indexed decision. (id=mem_idx) (pinned, used 3x)", content)
        self.assertIn("## Pending", content)
        self.assertIn("source of truth", content)


if __name__ == "__main__":
    unittest.main()
