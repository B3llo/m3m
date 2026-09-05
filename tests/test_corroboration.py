"""Corroboration tests: joining re-observed concepts instead of piling files.

Covers frontmatter compatibility, reconcile bands, the contradiction guard
(negation/contrast asymmetry, divergent numbers, antonyms), corroboration
accounting (no `updated`/content churn, idempotent retries, fail-closed
writes), ranking signal, remember/dedup guards, config bands, and the
versioned reconcile corpus gate. No provider calls: all LLM runners here
are scripted (this suite never touches any API key).
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from agent_m3m.config import Config
from agent_m3m.cli import _consolidation_candidates, _remember_memory, _run_dedup
from agent_m3m.extractor import MemoryExtractor
from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus, MemoryType
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore
from agent_m3m.textutils import contradiction_guard_tripped, jaccard, meaningful_tokens

CORPUS_PATH = Path(__file__).resolve().parent.parent / "evals" / "corroboration_cases.json"
RUNNER_PATH = Path(__file__).resolve().parent.parent / "evals" / "run_corroboration_eval.py"


def _temp_config(temp_dir):
    root = Path(temp_dir)
    return Config(global_path=root / "global", project_path=root / "project" / ".agent-m3m")


def _keeper(content, config, keeper_id="mem_keeper"):
    return MemoryItem(
        id=keeper_id,
        content=content,
        scope=MemoryScope.PROJECT,
        project_id=config.project_id,
        type=MemoryType.CONVENTION,
        status=MemoryStatus.ACTIVE,
        confidence=1.0,
    )


def _candidate(content, confidence=0.95):
    return MemoryItem(
        id="mem_candidate",
        content=content,
        scope=MemoryScope.PROJECT,
        type=MemoryType.CONVENTION,
        status=MemoryStatus.ACTIVE,
        confidence=confidence,
    )


class ScriptedRunner:
    """LLM stub: scripted extraction payload, scripted reconcile, call counts."""

    def __init__(self, memories=(), reconcile_action="ADD"):
        self.memories = list(memories)
        self.reconcile_action = reconcile_action
        self.calls = 0

    def run_prompt(self, *args, **kwargs):
        self.calls += 1
        prompt = args[0] if args else ""
        if "reconcile candidate memories" in prompt:
            rows = [{"candidate": index, "action": self.reconcile_action, "id": None}
                    for index in range(4)]
            return True, json.dumps({"decisions": rows})
        payload = {"memories": [dict(m) for m in self.memories]}
        return True, json.dumps(payload)


def _memory_payload(content, confidence=0.95):
    return {
        "content": content,
        "scope": "project",
        "type": "convention",
        "topic": "",
        "confidence": confidence,
        "tags": [],
        "entities": [],
        "contradicts_existing_id": None,
    }


class FrontmatterCompatibilityTests(unittest.TestCase):
    def test_new_fields_round_trip(self):
        item = MemoryItem(id="mem_x", content=" proves round trip.")
        item.corroborated_count = 7
        item.last_corroborated = "2026-09-01T00:00:00+00:00"
        restored = MemoryItem.from_markdown(item.to_markdown())
        self.assertEqual(restored.corroborated_count, 7)
        self.assertEqual(restored.last_corroborated, "2026-09-01T00:00:00+00:00")

    def test_legacy_files_default_to_zero_and_none(self):
        raw = "---\nid: \"mem_old\"\nscope: \"global\"\n---\n\nLegacy content.\n"
        restored = MemoryItem.from_markdown(raw)
        self.assertEqual(restored.corroborated_count, 0)
        self.assertIsNone(restored.last_corroborated)

    def test_unknown_keys_are_ignored(self):
        raw = "---\nid: \"mem_f\"\nscope: \"global\"\nfuture_field: \"whatever\"\n---\n\nBody.\n"
        restored = MemoryItem.from_markdown(raw)
        self.assertEqual(restored.content, "Body.")


class GuardUnitTests(unittest.TestCase):
    def test_asymmetric_negation_trips(self):
        self.assertTrue(contradiction_guard_tripped(
            "Use orbitpm for JavaScript dependency management.",
            "Do not use orbitpm for JavaScript dependency management."))
        self.assertTrue(contradiction_guard_tripped(
            "Pode aplicar a correção pontual.", "Não pode aplicar a correção pontual."))

    def test_symmetric_negation_does_not_trip(self):
        self.assertFalse(contradiction_guard_tripped(
            "Application processes must not run as root inside Docker containers.",
            "Docker containers must never run application processes as root."))
        self.assertFalse(contradiction_guard_tripped(
            "Não executar terraform apply sem aprovação explícita.",
            "Nunca executar terraform apply sem aprovação explícita."))

    def test_divergent_numbers_trip(self):
        self.assertTrue(contradiction_guard_tripped(
            "Redis session cache entries expire after 24 hours.",
            "Redis session cache entries expire after 48 hours."))
        self.assertTrue(contradiction_guard_tripped(
            "Use Python 3.12 for new services.", "Use Python 3.13 for new services."))

    def test_same_or_absent_numbers_do_not_trip(self):
        self.assertFalse(contradiction_guard_tripped(
            "Redis session cache entries expire after 24 hours.",
            "Session cache entries in Redis expire after 24 hours."))
        self.assertFalse(contradiction_guard_tripped(
            "Use orbitpm for JavaScript dependency management.",
            "JavaScript package management uses orbitpm."))

    def test_contrast_markers_and_antonyms_trip(self):
        self.assertTrue(contradiction_guard_tripped(
            "Use npm for this project.", "Use orbitpm instead of npm for this project."))
        self.assertTrue(contradiction_guard_tripped(
            "Usar poetry para dependências Python.", "Usar uv em vez de poetry para dependências Python."))
        self.assertTrue(contradiction_guard_tripped(
            "Allow direct pushes to the main branch.", "Forbid direct pushes to the main branch."))


class ReconcileBandTests(unittest.TestCase):
    def _extractor(self, config, runner):
        store = MemoryStore(config)
        return MemoryExtractor(store=store, llm_runner=runner, config=config), store

    def test_near_band_skips_without_llm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            runner = ScriptedRunner()
            extractor, _ = self._extractor(config, runner)
            keeper = _keeper("Redis session cache entries expire after 24 hours.", config)
            candidate = _candidate("Session cache entries in Redis expire after 24 hours.")
            decisions = extractor._reconcile_with_existing([candidate], [keeper])
            self.assertEqual(decisions[0]["action"], "SKIP")
            self.assertEqual(runner.calls, 0)
            self.assertEqual(extractor.last_auto_skips[0]["band"], "near_duplicate")

    def test_auto_band_skips_without_llm_and_records_band(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            runner = ScriptedRunner()
            extractor, _ = self._extractor(config, runner)
            keeper = _keeper("Run the complete test suite before pushing changes to the main branch.", config)
            candidate = _candidate("Always run the full test suite before pushing to the main branch.")
            score = jaccard(meaningful_tokens(keeper.content), meaningful_tokens(candidate.content))
            self.assertGreaterEqual(score, 0.60)
            self.assertLess(score, 0.80)
            decisions = extractor._reconcile_with_existing([candidate], [keeper])
            self.assertEqual(decisions[0]["action"], "SKIP")
            self.assertEqual(runner.calls, 0)
            self.assertEqual(extractor.last_auto_skips[0]["band"], "auto")

    def test_guarded_high_overlap_goes_to_llm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            runner = ScriptedRunner(reconcile_action="ADD")
            extractor, _ = self._extractor(config, runner)
            keeper = _keeper("Pode aplicar a correção pontual.", config)
            candidate = _candidate("Não pode aplicar a correção pontual.")
            decisions = extractor._reconcile_with_existing([candidate], [keeper])
            self.assertEqual(runner.calls, 1)
            self.assertEqual(decisions[0]["action"], "ADD")
            self.assertEqual(extractor.last_auto_skips, [])

    def test_pinned_keeper_never_auto_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            runner = ScriptedRunner(reconcile_action="ADD")
            extractor, _ = self._extractor(config, runner)
            keeper = _keeper("Run the complete test suite before pushing changes to the main branch.", config)
            keeper.pinned = True
            candidate = _candidate("Always run the full test suite before pushing to the main branch.")
            decisions = extractor._reconcile_with_existing([candidate], [keeper])
            self.assertEqual(runner.calls, 1)
            self.assertEqual(decisions[0]["action"], "ADD")


class CorroborationAccountingTests(unittest.TestCase):
    def _setup(self, temp_dir, keeper_content, candidate_content):
        config = _temp_config(temp_dir)
        store = MemoryStore(config)
        store.init_global()
        store.init_project()
        keeper = _keeper(keeper_content, config)
        store.save_item(keeper)
        return config, store, keeper

    def test_skip_corroborates_without_touching_content_or_updated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            keeper_content = "Run the complete test suite before pushing changes to the main branch."
            config, store, keeper = self._setup(
                temp_dir, keeper_content,
                "Always run the full test suite before pushing to the main branch.")
            before = Path(keeper.file_path).read_bytes()
            before_updated = keeper.updated
            runner = ScriptedRunner(memories=[_memory_payload(
                "Always run the full test suite before pushing to the main branch.")])
            extractor = MemoryExtractor(store=store, llm_runner=runner, config=config)
            saved = extractor.extract_from_transcript("run the full test suite before pushing.")
            self.assertEqual(saved, [])
            refreshed = store.load_item_by_id(keeper.id)
            self.assertEqual(refreshed.corroborated_count, 1)
            self.assertIsNotNone(refreshed.last_corroborated)
            self.assertEqual(refreshed.updated, before_updated)
            self.assertEqual(refreshed.content, keeper_content)
            self.assertEqual(refreshed.use_count, 0)
            after = Path(keeper.file_path).read_bytes()
            self.assertEqual(
                [line for line in after.decode().splitlines() if not line.startswith(("corroborated_count", "last_corroborated"))],
                [line for line in before.decode().splitlines() if not line.startswith(("corroborated_count", "last_corroborated"))],
            )
            reconciled = extractor.last_diagnostic["reconciled"]
            self.assertEqual(reconciled["corroborated"], 1)
            self.assertEqual(reconciled["auto_skips"][0]["keeper_id"], keeper.id)

    def test_idempotent_retry_counts_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, store, keeper = self._setup(
                temp_dir,
                "Run the complete test suite before pushing changes to the main branch.",
                "Always run the full test suite before pushing to the main branch.")
            runner = ScriptedRunner(memories=[_memory_payload(
                "Always run the full test suite before pushing to the main branch.")])
            extractor = MemoryExtractor(store=store, llm_runner=runner, config=config)
            first = extractor.extract_from_transcript("transcript one.", idempotency_key="job-1")
            second = extractor.extract_from_transcript("transcript one.", idempotency_key="job-1")
            self.assertEqual(first, [])
            self.assertEqual([item.id for item in second], [keeper.id])
            self.assertEqual(runner.calls, 1)
            self.assertEqual(store.load_item_by_id(keeper.id).corroborated_count, 1)

    def test_corroboration_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)

            class ExplodingStore(MemoryStore):
                def record_corroboration(self, *args, **kwargs):
                    raise OSError("disk on fire")

            store = ExplodingStore(config)
            store.init_global()
            store.init_project()
            keeper = _keeper("Run the complete test suite before pushing changes to the main branch.", config)
            store.save_item(keeper)
            runner = ScriptedRunner(memories=[_memory_payload(
                "Always run the full test suite before pushing to the main branch.")])
            extractor = MemoryExtractor(store=store, llm_runner=runner, config=config)
            saved = extractor.extract_from_transcript("run the full test suite before pushing.")
            self.assertEqual(saved, [])
            self.assertIn("corroboration_error", extractor.last_diagnostic["reconciled"])

    def test_skip_only_extraction_never_shrinks_the_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, store, keeper = self._setup(
                temp_dir,
                "Run the complete test suite before pushing changes to the main branch.",
                "Always run the full test suite before pushing to the main branch.")
            count_paths = lambda: len(list((store.project_dir / "conventions").glob("*.md")))
            before = count_paths()
            runner = ScriptedRunner(memories=[_memory_payload(
                "Always run the full test suite before pushing to the main branch.")])
            MemoryExtractor(store=store, llm_runner=runner, config=config).extract_from_transcript("x.")
            self.assertEqual(count_paths(), before)


class RememberGuardTests(unittest.TestCase):
    def test_remember_near_duplicate_corroborates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            retriever = MemoryRetriever(store, config)
            keeper = _keeper("Redis session cache entries expire after 24 hours.", config)
            store.save_item(keeper)
            saved, duplicate = _remember_memory(
                config, store, retriever,
                "Session cache entries in Redis expire after 24 hours.",
                MemoryScope.PROJECT, MemoryType.CONVENTION, [], [], None, False)
            self.assertIsNone(saved)
            self.assertEqual(duplicate.id, keeper.id)
            self.assertEqual(store.load_item_by_id(keeper.id).corroborated_count, 1)

    def test_remember_guarded_pair_saves_normally(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            retriever = MemoryRetriever(store, config)
            keeper = _keeper("Redis session cache entries expire after 24 hours.", config)
            store.save_item(keeper)
            saved, duplicate = _remember_memory(
                config, store, retriever,
                "Redis session cache entries expire after 48 hours.",
                MemoryScope.PROJECT, MemoryType.CONVENTION, [], [], None, False)
            self.assertIsNotNone(saved)
            self.assertIsNone(duplicate)


class DedupGuardTests(unittest.TestCase):
    def test_contradictory_pair_survives_apply(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            retriever = MemoryRetriever(store, config)
            store.save_item(_keeper("Redis session cache entries expire after 24 hours.", config, "mem_a"))
            store.save_item(_keeper("Redis session cache entries expire after 48 hours.", config, "mem_b"))
            _run_dedup(config, store, retriever, apply=True, threshold=0.60)
            self.assertEqual(store.load_item_by_id("mem_a").status, MemoryStatus.ACTIVE)
            self.assertEqual(store.load_item_by_id("mem_b").status, MemoryStatus.ACTIVE)

    def test_true_paraphrase_still_clusters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            retriever = MemoryRetriever(store, config)
            keeper = _keeper("Redis session cache entries expire after 24 hours.", config, "mem_k")
            keeper.use_count = 5
            store.save_item(keeper)
            dupe = _keeper("Session cache entries in Redis expire after 24 hours.", config, "mem_d")
            store.save_item(dupe)
            _run_dedup(config, store, retriever, apply=True, threshold=0.60)
            self.assertEqual(store.load_item_by_id("mem_d").status, MemoryStatus.SUPERSEDED)
            self.assertEqual(store.load_item_by_id("mem_k").status, MemoryStatus.ACTIVE)

    def test_keeper_prefers_corroborated_on_tie(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            retriever = MemoryRetriever(store, config)
            weak = _keeper("Redis session cache entries expire after 24 hours.", config, "mem_weak")
            store.save_item(weak)
            strong = _keeper("Session cache entries in Redis expire after 24 hours.", config, "mem_strong")
            strong.corroborated_count = 9
            store.save_item(strong)
            _run_dedup(config, store, retriever, apply=True, threshold=0.60)
            self.assertEqual(store.load_item_by_id("mem_strong").status, MemoryStatus.ACTIVE)
            self.assertEqual(store.load_item_by_id("mem_weak").status, MemoryStatus.SUPERSEDED)


class RankingSignalTests(unittest.TestCase):
    def test_corroboration_bonus_lifts_otherwise_equal_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            plain = _keeper("The frontend workspace uses orbitpm as the JavaScript package manager.", config, "mem_plain")
            store.save_item(plain)
            strong = _keeper("The frontend workspace uses orbitpm as the JavaScript package manager. Extra note.", config, "mem_strong")
            strong.corroborated_count = 20
            store.save_item(strong)
            retriever = MemoryRetriever(store, config)
            retriever.reindex()
            totals = {c.item.id: (c.total_score, c.breakdown.get("corroboration_bonus", 0))
                      for c in retriever.search("orbitpm JavaScript package manager frontend", top_k=5)}
            self.assertGreater(totals["mem_strong"][1], 0)
            self.assertEqual(totals["mem_plain"][1], 0)
            self.assertGreater(totals["mem_strong"][0], totals["mem_plain"][0])

    def test_schema_version_three_and_current(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            retriever = MemoryRetriever(store, config)
            self.assertEqual(retriever.reindex(), 0)
            self.assertTrue(retriever._is_current())
            with retriever._connection() as conn:
                version = conn.execute("SELECT value FROM index_state WHERE key = 'schema_version'").fetchone()["value"]
            self.assertEqual(version, "3")

    def test_consolidation_prefers_corroborated_on_tie(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_global()
            store.init_project()
            weak = _keeper("Weak singleton memory marker.", config, "mem_w")
            store.save_item(weak)
            strong = _keeper("Strong corroborated memory marker.", config, "mem_s")
            strong.corroborated_count = 4
            store.save_item(strong)
            ordered = [item.id for item in _consolidation_candidates(store, config, MemoryScope.PROJECT)]
            self.assertLess(ordered.index("mem_s"), ordered.index("mem_w"))


class ConfigBandTests(unittest.TestCase):
    def test_bands_load_and_keep_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            config.global_dir.mkdir(parents=True, exist_ok=True)
            config.near_duplicate_jaccard = 0.70
            config.auto_skip_jaccard = 0.99
            config.ambiguous_jaccard = 0.10
            config.save_config()
            reloaded = Config(global_path=config.global_dir, project_path=config.project_dir)
            self.assertEqual(reloaded.near_duplicate_jaccard, 0.70)
            self.assertEqual(reloaded.auto_skip_jaccard, 0.70)
            self.assertEqual(reloaded.ambiguous_jaccard, 0.10)


class CorpusGateTests(unittest.TestCase):
    def test_versioned_reconcile_corpus_passes_all_gates(self):
        spec = importlib.util.spec_from_file_location("m3m_corrob_eval", RUNNER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = module.run_eval()
        self.assertEqual(report["schema_version"], 1)
        self.assertGreaterEqual(report["cases"], 70)
        self.assertEqual(report["adversarial_safe_rate"], 1.0)
        self.assertEqual(report["close_skip_rate"], 1.0)
        self.assertEqual(report["identical_skip_rate"], 1.0)
        self.assertEqual(report["loose_safe_rate"], 1.0)


class SystematicVariantTests(unittest.TestCase):
    """Mutation matrix over one keeper: every variant must resolve safely."""

    KEEPER = "The billing-api service settles Pix refunds within one business day."

    VARIANTS = [
        # (candidate, expect_skip_silent, expect_guard_trip)
        ("Pix refunds in the billing-api service settle within one business day.", True, False),
        ("within one business day the billing-api service settles Pix refunds", True, False),
        ("The billing-api service settles Pix refunds within two business days.", False, True),
        ("The billing-api service does not settle Pix refunds.", False, True),
        ("The billing-api service settles Pix refunds within one business month.", False, True),
        ("O serviço billing-api liquida reembolsos Pix em um dia útil.", False, False),
        ("Use orbitpm for JavaScript dependency management.", False, False),
        ("", False, False),
        ("   ", False, False),
    ]

    def test_variant_matrix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            for candidate_text, expect_skip, expect_trip in self.VARIANTS:
                with self.subTest(candidate=candidate_text[:40]):
                    runner = ScriptedRunner(reconcile_action="ADD")
                    extractor = MemoryExtractor(
                        store=MemoryStore(config), llm_runner=runner, config=config)
                    keeper = _keeper(self.KEEPER, config)
                    candidate = _candidate(candidate_text)
                    decisions = extractor._reconcile_with_existing([candidate], [keeper])
                    action = decisions[0]["action"]
                    self.assertEqual(
                        contradiction_guard_tripped(self.KEEPER, candidate_text), expect_trip)
                    if expect_skip:
                        self.assertEqual(action, "SKIP")
                        self.assertEqual(runner.calls, 0)
                        self.assertEqual(decisions[0]["id"], keeper.id)
                    else:
                        self.assertNotEqual(
                            (action, runner.calls), ("SKIP", 0),
                            "silent skip on a non-paraphrase variant")

    def test_structural_invariants_hold_over_full_corpus(self):
        payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            runner = ScriptedRunner(reconcile_action="ADD")
            extractor = MemoryExtractor(
                store=MemoryStore(config), llm_runner=runner, config=config)
            for case in payload["cases"]:
                with self.subTest(case=case["id"]):
                    keeper = _keeper(case["keeper"], config, f"k_{case['id']}")
                    candidate = _candidate(case["candidate"])
                    decisions = extractor._reconcile_with_existing([candidate], [keeper])
                    self.assertEqual(len(decisions), 1)
                    action = decisions[0]["action"]
                    self.assertIn(action, {"ADD", "SKIP", "UPDATE", "SUPERSEDE"})
                    if action == "SKIP":
                        # A silent skip must always name its keeper.
                        self.assertEqual(decisions[0]["id"], keeper.id)
                        self.assertEqual(keeper.scope, candidate.scope)


if __name__ == "__main__":
    unittest.main()
