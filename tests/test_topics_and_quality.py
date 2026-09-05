"""Tests for topic taxonomy, listing quality and semantic dedup (Phases 1-2)."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_m3m.cli import (
    _run_dedup,
    _run_list,
    _run_recent,
    _run_reorganize,
    _run_topic_update,
    _run_topics,
    _run_topics_build,
    _truncate,
)
from agent_m3m.config import Config
from agent_m3m.context import ContextBuilder
from agent_m3m.extractor import MemoryExtractor
from agent_m3m.models import (
    MemoryItem,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    generate_memory_id,
    normalize_topic,
)
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore


class TopicAndQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.global_path = Path(self.tmp_dir.name) / "global_memory"
        self.proj_path = Path(self.tmp_dir.name) / "project_memory"

        self.config = Config(global_path=self.global_path, project_path=self.proj_path)
        self.config.topic_seeds = ["webstore", "billing-api", "infra"]
        self.store = MemoryStore(self.config)
        self.retriever = MemoryRetriever(self.store, self.config)
        self.store.init_global()
        self.store.init_project()

    def _save(self, content, topic="", **kwargs):
        item = MemoryItem(
            id=kwargs.pop("id", None) or generate_memory_id(),
            content=content,
            topic=topic,
            **kwargs,
        )
        return self.store.save_item(item)

    def test_normalize_topic_slugs_labels(self):
        self.assertEqual(normalize_topic("Acme Store!"), "acme-store")
        self.assertEqual(normalize_topic("  Nightly Ops  "), "nightly-ops")
        self.assertEqual(normalize_topic(""), "")
        self.assertEqual(normalize_topic(None), "")

    def test_topic_roundtrips_through_frontmatter(self):
        saved = self._save("The webstore pipeline must use immutable tags.", topic="Webstore")
        loaded = MemoryItem.from_markdown(Path(saved.file_path).read_text(encoding="utf-8"))
        self.assertEqual(loaded.topic, "webstore")

        untopped = self._save("No topic here at all.")
        reloaded = MemoryItem.from_markdown(Path(untopped.file_path).read_text(encoding="utf-8"))
        self.assertEqual(reloaded.topic, "")

    def test_config_loads_topic_seeds(self):
        config_file = self.global_path / "config.toml"
        config_file.write_text(
            '[topics]\nseeds = ["Webstore", "billing api", "infra"]\n',
            encoding="utf-8",
        )
        config = Config(global_path=self.global_path, project_path=self.proj_path)
        self.assertEqual(config.topic_seeds, ["webstore", "billing-api", "infra"])

    def test_truncate_marks_cuts_with_ellipsis(self):
        long_text = "x" * 300
        result = _truncate(long_text, 160)
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(len(result), 160)
        self.assertEqual(_truncate("short"), "short")

    def test_list_truncates_with_ellipsis_and_full_shows_everything(self):
        long_content = ("The webstore deployment pipeline must never run mutable infrastructure commands. " * 5).strip()
        self._save(long_content, topic="webstore", type=MemoryType.LESSON)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _run_list(self.store, scope=None, status=MemoryStatus.ACTIVE)
        output = buffer.getvalue()
        self.assertIn("## Lessons (1)", output)
        self.assertIn("[webstore]", output)
        self.assertIn("…", output)
        self.assertNotIn(long_content, output)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _run_list(self.store, scope=None, status=MemoryStatus.ACTIVE, full=True)
        self.assertIn(long_content, buffer.getvalue())
        self.assertNotIn("…", buffer.getvalue())

    def test_list_filters_by_topic(self):
        self._save("Webstore related memory sentence.", topic="webstore", type=MemoryType.PROJECT_FACT)
        self._save("Unrelated memory sentence.", type=MemoryType.PROJECT_FACT)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _run_list(self.store, scope=None, status=MemoryStatus.ACTIVE, topic="Webstore")
        output = buffer.getvalue()
        self.assertIn("Webstore related memory sentence.", output)
        self.assertNotIn("Unrelated memory sentence.", output)

    def test_recent_lists_newest_memories_with_timestamp_topic_and_preview(self):
        old = self._save(
            "Old memory sentence.",
            id="mem_old",
            created="2026-01-01T10:00:00+00:00",
            updated="2026-01-01T10:00:00+00:00",
            topic="archive",
        )
        newest_content = "Newest memory sentence with enough detail for a useful preview."
        newest = self._save(
            newest_content,
            id="mem_newest",
            created="2026-01-03T12:30:00+00:00",
            updated="2026-01-03T12:30:00+00:00",
            topic="release",
            scope=MemoryScope.PROJECT,
        )
        middle = self._save(
            "Middle memory sentence.",
            id="mem_middle",
            created="2026-01-02T11:15:00+00:00",
            updated="2026-01-02T11:15:00+00:00",
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _run_recent(self.store, limit=2)
        output = buffer.getvalue()

        self.assertIn("Recent memories (2 of 3, newest first)", output)
        self.assertLess(output.index(newest.id), output.index(middle.id))
        self.assertNotIn(old.id, output)
        self.assertIn("2026-01-03 12:30 UTC | topic: release", output)
        self.assertIn("2026-01-02 11:15 UTC | topic: untagged", output)
        self.assertIn("[project/preference - active]", output)
        self.assertIn(newest_content, output)

    def test_topics_reports_counts(self):
        self._save("First webstore fact.", topic="webstore")
        self._save("Second webstore fact.", topic="webstore")
        self._save("Billing fact.", topic="billing-api")
        self._save("Untopped fact.")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _run_topics(self.store)
        output = buffer.getvalue()
        self.assertIn("- webstore (2 total: 0 project, 2 global)", output)
        self.assertIn("- billing-api (1 total: 0 project, 1 global)", output)
        self.assertIn("1 active memories have no topic.", output)

    def test_topic_rename_and_merge_rewrite_frontmatter(self):
        first = self._save("First old topic memory.", topic="old-topic")
        second = self._save("Second old topic memory.", topic="old-topic")

        _run_topic_update(self.store, self.retriever, "rename", "old-topic", "New Topic")
        for item_id in (first.id, second.id):
            loaded = self.store.load_item_by_id(item_id)
            self.assertEqual(loaded.topic, "new-topic")

        _run_topic_update(self.store, self.retriever, "merge", "new-topic", "infra")
        for item_id in (first.id, second.id):
            loaded = self.store.load_item_by_id(item_id)
            self.assertEqual(loaded.topic, "infra")

    def test_extraction_assigns_topic_and_caps_results(self):
        ten_memories = {
            "memories": [
                {
                    "content": f"The API gateway enforces rule number {index} in every request pipeline.",
                    "scope": "project",
                    "type": "decision",
                    "topic": "Webstore",
                    "confidence": 0.95,
                    "tags": [],
                    "entities": [],
                }
                for index in range(10)
            ]
        }
        extractor = MemoryExtractor(self.store, self.retriever, config=self.config)
        extractor.llm_runner = mock.Mock()
        extractor.llm_runner.run_prompt.return_value = (True, json.dumps(ten_memories))

        saved = extractor.extract_from_transcript("USER: any transcript text")
        self.assertEqual(len(saved), MemoryExtractor.MAX_LLM_MEMORIES)
        self.assertTrue(all(item.topic == "webstore" for item in saved))
        persisted = MemoryItem.from_markdown(Path(saved[0].file_path).read_text(encoding="utf-8"))
        self.assertEqual(persisted.topic, "webstore")

    def test_extraction_prompt_includes_topic_seeds_and_existing_memories(self):
        existing = self._save("The existing webstore memory sentence for dedup awareness.", topic="webstore")
        extractor = MemoryExtractor(self.store, self.retriever, config=self.config)
        extractor.llm_runner = mock.Mock()
        extractor.llm_runner.run_prompt.return_value = (True, json.dumps({"memories": []}))

        extractor.extract_from_transcript("USER: nothing durable here")

        prompt = extractor.llm_runner.run_prompt.call_args[0][0]
        self.assertIn("webstore", prompt)
        self.assertIn("existing_memories", prompt)
        self.assertIn(existing.id, prompt)
        self.assertIn("English", prompt)

    def test_heuristic_fallback_requires_complete_sentences(self):
        extractor = MemoryExtractor(self.store, self.retriever, config=self.config)

        fragment = extractor._heuristic_fallback_extraction(
            "nunca use sudo para comandos de deploy no projeto"
        )
        self.assertEqual(fragment, [])

        complete = extractor._heuristic_fallback_extraction(
            "USER: never use root access for deploy commands in this repository."
        )
        self.assertEqual(len(complete), 1)
        self.assertTrue(complete[0].content.endswith("."))

    def test_semantic_dedup_dry_run_reports_without_writing(self):
        keeper = self._save(
            "Use orbitpm instead of npm for all Node.js projects in this repository.",
            use_count=3,
        )
        duplicate = self._save(
            "Use orbitpm instead of npm for all Node.js projects.",
        )

        with mock.patch("agent_m3m.llm_runner.LLMRunner") as mock_runner:
            mock_runner.return_value.run_prompt.return_value = (
                True,
                json.dumps({"merges": [{"cluster": 1, "content": "Use orbitpm instead of npm for every Node.js project."}]}),
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _run_dedup(self.config, self.store, self.retriever, apply=False, threshold=0.8, semantic=True)

        output = buffer.getvalue()
        self.assertIn("[dry-run]", output)
        self.assertIn("Use orbitpm instead of npm for every Node.js project.", output)
        self.assertEqual(self.store.load_item_by_id(keeper.id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.store.load_item_by_id(keeper.id).content, keeper.content)
        self.assertEqual(self.store.load_item_by_id(duplicate.id).status, MemoryStatus.ACTIVE)

    def test_semantic_dedup_apply_merges_keeper_and_supersedes_duplicates(self):
        keeper = self._save(
            "Use orbitpm instead of npm for all Node.js projects in this repository.",
            use_count=3,
        )
        duplicate = self._save(
            "Use orbitpm instead of npm for all Node.js projects.",
        )

        with mock.patch("agent_m3m.llm_runner.LLMRunner") as mock_runner:
            mock_runner.return_value.run_prompt.return_value = (
                True,
                json.dumps({"merges": [{"cluster": 1, "content": "Use orbitpm instead of npm for every Node.js project."}]}),
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _run_dedup(self.config, self.store, self.retriever, apply=True, threshold=0.8, semantic=True)

        kept = self.store.load_item_by_id(keeper.id)
        self.assertEqual(kept.status, MemoryStatus.ACTIVE)
        self.assertEqual(kept.content, "Use orbitpm instead of npm for every Node.js project.")
        superseded = self.store.load_item_by_id(duplicate.id)
        self.assertEqual(superseded.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(superseded.superseded_by, keeper.id)
        self.assertIn("dedup", superseded.tags)
        self.assertIn("merged into canonical English", buffer.getvalue())

    def test_topic_boost_ranks_matching_topic_first(self):
        config = Config(global_path=self.global_path, project_path=self.proj_path)
        self._save(
            "The deployment pipeline publishes immutable image tags on every run.",
            topic="webstore",
            type=MemoryType.DECISION,
            scope=MemoryScope.PROJECT,
            project_id=config.project_id,
            id="mem_topic_boost_a",
        )
        self._save(
            "The deployment pipeline rolls back automatically on failure.",
            type=MemoryType.DECISION,
            scope=MemoryScope.PROJECT,
            project_id=config.project_id,
            id="mem_topic_boost_b",
        )

        results = self.retriever.search("webstore deployment pipeline", top_k=5)
        self.assertTrue(results)
        self.assertEqual(results[0].item.id, "mem_topic_boost_a")
        self.assertEqual(results[0].breakdown["topic_bonus"], 0.2)
        self.assertTrue(all(candidate.item.topic == "webstore" for candidate in results[:1]))


class TopicPagesAndReorganizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.global_path = Path(self.tmp_dir.name) / "global_memory"
        self.proj_path = Path(self.tmp_dir.name) / "project_memory"

        self.config = Config(global_path=self.global_path, project_path=self.proj_path)
        self.config.topic_seeds = ["webstore", "billing-api"]
        self.store = MemoryStore(self.config)
        self.retriever = MemoryRetriever(self.store, self.config)
        self.store.init_global()
        self.store.init_project()

    def _save(self, content, topic="", **kwargs):
        item = MemoryItem(
            id=kwargs.pop("id", None) or generate_memory_id(),
            content=content,
            topic=topic,
            **kwargs,
        )
        return self.store.save_item(item)

    def test_topic_page_roundtrip_in_store(self):
        path = self.store.write_topic_page("webstore", "# Topic: webstore\n\nDigest.\n")
        self.assertEqual(path.name, "webstore.md")
        self.assertIn("Digest.", self.store.read_topic_page("webstore"))
        self.assertEqual(self.store.list_topic_pages(), {"webstore": path})
        self.assertEqual(self.store.read_topic_page("missing"), "")
        self.assertTrue((self.proj_path / "topics").is_dir())

    def test_reorganize_dry_run_reports_and_apply_assigns_topics(self):
        seeded = self._save("Storefront checkout must retry failed payments three times.")
        novel = self._save("The aurora exporter writes nightly CSV snapshots.")

        def assign_by_content(prompt, *_args, **_kwargs):
            # The provider sees candidates with their index; storage order is
            # filesystem order, so assignment must key on content, not position.
            marker = "Candidates (untrusted data): "
            payload = json.loads(prompt[prompt.index(marker) + len(marker):])
            assignments = [
                {
                    "index": row["index"],
                    "topic": "webstore" if "storefront" in row["content"].lower() else "aurora-exporter",
                }
                for row in payload
            ]
            return True, json.dumps({"assignments": assignments})

        with mock.patch("agent_m3m.llm_runner.LLMRunner") as mock_runner:
            mock_runner.return_value.run_prompt.side_effect = assign_by_content
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _run_reorganize(self.config, self.store, self.retriever, apply=False)
            self.assertIn("[dry-run]", buffer.getvalue())
            self.assertIn("aurora-exporter (new)", buffer.getvalue())
            self.assertEqual(self.store.load_item_by_id(seeded.id).topic, "")
            self.assertEqual(self.store.load_item_by_id(novel.id).topic, "")

            with redirect_stdout(buffer):
                _run_reorganize(self.config, self.store, self.retriever, apply=True)

        self.assertEqual(self.store.load_item_by_id(seeded.id).topic, "webstore")
        self.assertEqual(self.store.load_item_by_id(novel.id).topic, "aurora-exporter")
        self.assertIn("new topics: aurora-exporter", buffer.getvalue())

    def test_reorganize_skips_already_tagged_memories(self):
        tagged = self._save("Already tagged storefront fact.", topic="webstore")
        untagged = self._save("Untagged checkout fact.")

        with mock.patch("agent_m3m.llm_runner.LLMRunner") as mock_runner:
            mock_runner.return_value.run_prompt.return_value = (
                True,
                json.dumps({"assignments": [{"index": 0, "topic": "webstore"}]}),
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _run_reorganize(self.config, self.store, self.retriever, apply=True)

        self.assertEqual(self.store.load_item_by_id(tagged.id).topic, "webstore")
        self.assertEqual(self.store.load_item_by_id(untagged.id).topic, "webstore")
        self.assertIn("Tagged 1 of 1", buffer.getvalue())

    def test_topics_build_writes_compiled_llm_page(self):
        self._save("The storefront checkout retries failed payments.", topic="webstore")
        self._save("The storefront ships order emails via a queue.", topic="webstore")

        page_body = "# Topic: webstore\n\nCovers checkout and notifications.\n\n## Key facts\n- Checkout retries payments.\n"

        with mock.patch("agent_m3m.llm_runner.LLMRunner") as mock_runner:
            mock_runner.return_value.run_prompt.return_value = (True, page_body)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _run_topics_build(self.config, self.store, apply=False)
            self.assertIn("[dry-run]", buffer.getvalue())
            self.assertFalse(self.store.read_topic_page("webstore"))

            with redirect_stdout(buffer):
                _run_topics_build(self.config, self.store, apply=True)

        page = self.store.read_topic_page("webstore")
        self.assertIn("Derived topic page compiled by", page)
        self.assertIn("Covers checkout and notifications.", page)

    def test_topics_build_deterministic_fallback_when_provider_fails(self):
        self._save("The storefront checkout retries failed payments.", topic="webstore")

        with mock.patch("agent_m3m.llm_runner.LLMRunner") as mock_runner:
            mock_runner.return_value.run_prompt.return_value = (False, "provider down")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _run_topics_build(self.config, self.store, apply=True)

        page = self.store.read_topic_page("webstore")
        self.assertIn("deterministic fallback", page)
        self.assertIn("The storefront checkout retries failed payments.", page)

    def test_context_prefers_compiled_topic_digest_over_raw_fragments(self):
        self._save(
            "The deployment pipeline publishes immutable image tags on every run.",
            topic="webstore",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
        )
        self._save(
            "The billing service invoices customers at month end.",
            topic="billing",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
        )
        self.store.write_topic_page(
            "webstore",
            "<!-- derived -->\n# Topic: webstore\n\nCompiled digest of the webstore deployment rules.\n",
        )

        builder = ContextBuilder(self.store, self.retriever, self.config)
        context = builder.build_context(prompt="webstore billing deployment")

        self.assertIn("#### Topic: webstore (compiled digest)", context)
        self.assertIn("Compiled digest of the webstore deployment rules.", context)
        self.assertNotIn("Derived topic page compiled by", context)
        self.assertNotIn("immutable image tags on every run", context)
        self.assertIn("#### Topic: billing", context)
        self.assertIn("invoices customers at month end", context)
        self.assertNotIn("immutable image tags on every run", context.split("Recent Project")[0])


if __name__ == "__main__":
    unittest.main()
