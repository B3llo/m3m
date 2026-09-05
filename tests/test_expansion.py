"""Query-expansion tests: prefixes + multilingual synonym map (two-tier search).

Tier 1 (strict FTS) is untouched: expansion only adds candidates when the
strict tiers underfill. All fixtures are local; no provider calls.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from agent_m3m.config import Config
from agent_m3m.models import MemoryItem, MemoryScope
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore
from agent_m3m.textutils import (
    build_synonym_map,
    expand_query_terms,
    fold_accents,
    parse_synonyms_md,
)

RUNNER_PATH = Path(__file__).resolve().parent.parent / "evals" / "run_semantic_eval.py"


def _temp_config(temp_dir):
    root = Path(temp_dir)
    return Config(global_path=root / "global", project_path=root / "project" / ".agent-m3m")


def _seed_store(config, memories):
    store = MemoryStore(config)
    store.init_project()
    for index, content in enumerate(memories):
        store.save_item(MemoryItem(
            id=f"exp_{index}",
            content=content,
            scope=MemoryScope.PROJECT,
            project_id=config.project_id,
            confidence=1.0,
        ))
    return store


class SynonymMapTests(unittest.TestCase):
    def test_bidirectional_en_pt_lookup(self):
        synonym_map = build_synonym_map()
        self.assertIn("refund", synonym_map.get("reembolso", []))
        self.assertIn("reembolso", synonym_map.get("refund", []))
        self.assertIn("approval", synonym_map.get("aprovacao", []))

    def test_other_languages_bridge_through_english(self):
        synonym_map = build_synonym_map()
        self.assertIn("deadline", synonym_map.get("plazo", []))
        self.assertIn("refund", synonym_map.get("reembolso", []))
        self.assertIn("approval", synonym_map.get("genehmigung", []))
        self.assertIn("queue", synonym_map.get("coda", []))

    def test_accent_insensitive_keys(self):
        synonym_map = build_synonym_map()
        self.assertIn("password", synonym_map.get("contrasena", []))
        self.assertEqual(fold_accents("aprovação"), "aprovacao")

    def test_false_friends_stay_out(self):
        synonym_map = build_synonym_map()
        self.assertNotIn("file", synonym_map)

    def test_overlay_parsing_ignores_noise(self):
        groups = parse_synonyms_md(
            "# team synonyms\n"
            "deployar: deploy, rollout\n"
            "malformed line without colon\n"
            "lonely:\n"
            "\n"
            "cache, esconderijo: redis\n"
        )
        synonym_map = build_synonym_map(groups)
        self.assertIn("deploy", synonym_map.get("deployar", []))
        self.assertIn("redis", synonym_map.get("cache", []))

    def test_prefix_terms_are_bare_and_safe(self):
        terms = expand_query_terms(["reembolso", "OR", "aprovacao"], build_synonym_map())
        bare = [term for term in terms if term.endswith("*")]
        self.assertTrue(bare)
        self.assertFalse(any(term.rstrip("*").upper() in {"AND", "OR", "NOT", "NEAR"} for term in bare))
        quoted = [term for term in terms if term.startswith('"')]
        self.assertTrue(any("refund" in term for term in quoted))

    def test_multiword_keys_expand_from_phrases(self):
        terms = expand_query_terms(["banco", "de", "dados"], build_synonym_map())
        self.assertTrue(any("database" in term for term in terms))


class TwoTierSearchTests(unittest.TestCase):
    MEMORIES = [
        "The billing service settles Pix refunds within one business day.",
        "Database credentials live in the vault, never in code.",
    ]

    def test_zero_overlap_pair_found_only_with_expansion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = _seed_store(config, self.MEMORIES)
            retriever = MemoryRetriever(store, config)
            retriever.reindex()
            config.query_expansion = False
            off = [c.item.id for c in retriever.search("qual o prazo de reembolso", top_k=5)]
            config.query_expansion = True
            on = [c.item.id for c in retriever.search("qual o prazo de reembolso", top_k=5)]
            self.assertNotIn("exp_0", off)
            self.assertIn("exp_0", on)

    def test_strict_fill_is_unchanged_by_expansion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = _seed_store(config, self.MEMORIES)
            retriever = MemoryRetriever(store, config)
            retriever.reindex()
            config.query_expansion = False
            off = [c.item.id for c in retriever.search("Pix refunds billing", top_k=5)]
            config.query_expansion = True
            on = [c.item.id for c in retriever.search("Pix refunds billing", top_k=5)]
            self.assertEqual(off, on)
            self.assertEqual(off[0], "exp_0")

    def test_project_overlay_adds_custom_synonyms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = _seed_store(config, ["The team deploys with orbitpm daily."])
            (config.project_dir / "synonyms.md").write_text(
                "# team words\ndeployar: orbitpm\n", encoding="utf-8")
            retriever = MemoryRetriever(store, config)
            retriever.reindex()
            ids = [c.item.id for c in retriever.search("como deployar hoje", top_k=5)]
            self.assertIn("exp_0", ids)

    def test_out_of_domain_query_stays_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = _seed_store(config, self.MEMORIES)
            retriever = MemoryRetriever(store, config)
            retriever.reindex()
            self.assertEqual(retriever.search("receita de bolo de cenoura", top_k=5), [])

    def test_expansion_only_appends_never_reorders(self):
        """Recall-only guarantee: ids found with expansion off keep their
        relative order with expansion on; expansion only appends past them."""
        import json

        corpus = json.loads(
            (Path(__file__).resolve().parent.parent / "evals" / "retrieval_cases.json")
            .read_text(encoding="utf-8"))
        semantic = json.loads(
            (Path(__file__).resolve().parent.parent / "evals" / "semantic_cases.json")
            .read_text(encoding="utf-8"))
        queries = [case["query"] for case in corpus["cases"]]
        queries.extend(case["query"] for case in semantic["positive"])
        queries.extend(case["query"] for case in semantic["negative"])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            memories = [case["memory"] for case in corpus["cases"]]
            memories.extend(case["memory"] for case in semantic["positive"] if "memory" in case)
            store = MemoryStore(config)
            store.init_project()
            for index, content in enumerate(memories):
                store.save_item(MemoryItem(
                    id=f"mix_{index}", content=content,
                    scope=MemoryScope.PROJECT, project_id=config.project_id, confidence=1.0))
            retriever = MemoryRetriever(store, config)
            retriever.reindex()
            for query in queries:
                with self.subTest(query=query[:40]):
                    config.query_expansion = False
                    off = [c.item.id for c in retriever.search(query, top_k=5)]
                    config.query_expansion = True
                    on = [c.item.id for c in retriever.search(query, top_k=5)]
                    off_set = set(off)
                    self.assertEqual([i for i in on if i in off_set], off)
                    for item_id in off:
                        self.assertIn(item_id, on)

    def test_strict_match_outranks_expansion_only_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            store = MemoryStore(config)
            store.init_project()
            # Short competitor matches only via the "index" synonym...
            store.save_item(MemoryItem(
                id="mem_short", content="Create PostgreSQL indexes concurrently.",
                scope=MemoryScope.PROJECT, project_id=config.project_id, confidence=1.0))
            # ...while the right memory matches the literal word "downtime".
            store.save_item(MemoryItem(
                id="mem_full",
                content="The billing-api production migrations create PostgreSQL indexes concurrently without downtime.",
                scope=MemoryScope.PROJECT, project_id=config.project_id, confidence=1.0))
            retriever = MemoryRetriever(store, config)
            retriever.reindex()
            ids = [c.item.id for c in retriever.search(
                "criar índice Postgres em produção sem downtime", top_k=5)]
            self.assertIn("mem_full", ids)
            self.assertLess(ids.index("mem_full"), ids.index("mem_short") if "mem_short" in ids else len(ids))

    def test_config_flag_round_trips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _temp_config(temp_dir)
            config.global_dir.mkdir(parents=True, exist_ok=True)
            config.query_expansion = False
            config.save_config()
            reloaded = Config(global_path=config.global_dir, project_path=config.project_dir)
            self.assertFalse(reloaded.query_expansion)


class SemanticCorpusGateTests(unittest.TestCase):
    def test_hard_corpus_passes_gates(self):
        spec = importlib.util.spec_from_file_location("m3m_semantic_eval", RUNNER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = module.run_eval()
        self.assertEqual(report["schema_version"], 1)
        self.assertGreaterEqual(report["positive"], 10)
        self.assertEqual(report["hard_recall_at_k"], 1.0)
        self.assertEqual(report["negative_leak_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
