#!/usr/bin/env python3
"""Run the versioned deterministic retrieval quality evaluation."""

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from agent_m3m.config import Config
from agent_m3m.models import MemoryItem, MemoryScope
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore


DEFAULT_CASES = Path(__file__).with_name("retrieval_cases.json")


def run_eval(cases_path: Path = DEFAULT_CASES, top_k: int = 5) -> Dict[str, Any]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("cases"), list):
        raise ValueError("Unsupported retrieval evaluation schema")
    cases: List[Dict[str, str]] = payload["cases"]
    if not cases:
        raise ValueError("Retrieval evaluation has no cases")

    with tempfile.TemporaryDirectory(prefix="m3m-retrieval-eval-") as temp_dir:
        root = Path(temp_dir)
        config = Config(global_path=root / "global", project_path=root / "project" / ".agent-m3m")
        store = MemoryStore(config)
        store.init_project()
        for case in cases:
            store.save_item(MemoryItem(
                id=f"eval_{case['id']}",
                content=case["memory"],
                scope=MemoryScope.PROJECT,
                project_id=config.project_id,
                confidence=1.0,
            ))

        retriever = MemoryRetriever(store, config)
        retriever.reindex()
        rows = []
        reciprocal_rank_total = 0.0
        hits = 0
        for case in cases:
            expected = f"eval_{case['id']}"
            result_ids = [candidate.item.id for candidate in retriever.search(case["query"], top_k=top_k)]
            rank = result_ids.index(expected) + 1 if expected in result_ids else None
            if rank is not None:
                hits += 1
                reciprocal_rank_total += 1.0 / rank
            rows.append({"id": case["id"], "expected": expected, "rank": rank, "results": result_ids})

    count = len(cases)
    return {
        "schema_version": 1,
        "cases": count,
        "top_k": top_k,
        "recall_at_k": hits / count,
        "mrr": reciprocal_rank_total / count,
        "results": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-recall-at-5", type=float, default=1.0)
    parser.add_argument("--min-mrr", type=float, default=0.8)
    args = parser.parse_args()
    report = run_eval(args.cases, top_k=args.top_k)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["recall_at_k"] < args.min_recall_at_5 or report["mrr"] < args.min_mrr:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
