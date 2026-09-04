"""Quality gate for the versioned retrieval evaluation corpus."""

import importlib.util
import unittest
from pathlib import Path


RUNNER_PATH = Path(__file__).parents[1] / "evals" / "run_retrieval_eval.py"
SPEC = importlib.util.spec_from_file_location("m3m_retrieval_eval", RUNNER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f"Could not load retrieval evaluation runner from {RUNNER_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RetrievalEvalTests(unittest.TestCase):
    def test_versioned_corpus_meets_quality_gate(self):
        report = MODULE.run_eval()
        self.assertEqual(report["schema_version"], 1)
        self.assertGreaterEqual(report["cases"], 8)
        self.assertGreaterEqual(report["recall_at_k"], 1.0)
        self.assertGreaterEqual(report["mrr"], 0.8)


if __name__ == "__main__":
    unittest.main()
