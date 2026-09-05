#!/usr/bin/env python3
"""Run the zero-overlap cross-language retrieval evaluation.

Positive cases share no content tokens with their memories: the strict FTS
tier finds nothing and the expansion tier (prefixes + synonym map) must
rescue them. Negative cases are out-of-domain queries that must surface no
hard memory (precision guard against over-expansion).
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_m3m.config import Config
from agent_m3m.models import MemoryItem, MemoryScope
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore

DEFAULT_CASES = Path(__file__).with_name("semantic_cases.json")


def run_eval(cases_path=DEFAULT_CASES, top_k=5):
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported semantic evaluation schema")
    positives = payload.get("positive", [])
    negatives = payload.get("negative", [])
    if not positives:
        raise ValueError("Semantic evaluation has no positive cases")

    with tempfile.TemporaryDirectory(prefix="m3m-semantic-eval-") as temp_dir:
        root = Path(temp_dir)
        config = Config(global_path=root / "global", project_path=root / "project" / ".agent-m3m")
        store = MemoryStore(config)
        store.init_project()
        memory_ids = {}
        for case in positives:
            if "memory" not in case:
                continue
            item_id = f"sem_{case['id']}"
            store.save_item(MemoryItem(
                id=item_id,
                content=case["memory"],
                scope=MemoryScope.PROJECT,
                project_id=config.project_id,
                confidence=1.0,
            ))
            memory_ids[case["id"]] = item_id

        retriever = MemoryRetriever(store, config)
        retriever.reindex()

        rows = []
        hits = 0
        for case in positives:
            expected = memory_ids.get(case["id"]) or memory_ids.get(case.get("memory_id", ""))
            result_ids = [c.item.id for c in retriever.search(case["query"], top_k=top_k)]
            rank = result_ids.index(expected) + 1 if expected in result_ids else None
            if rank is not None:
                hits += 1
            rows.append({"id": case["id"], "expected": expected, "rank": rank, "results": result_ids})

        neg_rows = []
        leaks = 0
        hard_ids = set(memory_ids.values())
        for case in negatives:
            result_ids = [c.item.id for c in retriever.search(case["query"], top_k=top_k)]
            leaked = sorted(hard_ids & set(result_ids))
            leaks += len(leaked)
            neg_rows.append({"id": case["id"], "results": result_ids, "leaked": leaked})

    return {
        "schema_version": 1,
        "positive": len(positives),
        "negative": len(negatives),
        "top_k": top_k,
        "hard_recall_at_k": hits / len(positives),
        "negative_leak_rate": leaks / len(negatives) if negatives else 0.0,
        "results": rows,
        "negative_results": neg_rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-hard-recall-at-5", type=float, default=1.0)
    parser.add_argument("--max-negative-leak-rate", type=float, default=0.0)
    args = parser.parse_args()
    report = run_eval(args.cases, top_k=args.top_k)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    failed = []
    if report["hard_recall_at_k"] < args.min_hard_recall_at_5:
        failed.append("hard_recall_at_k")
    if report["negative_leak_rate"] > args.max_negative_leak_rate:
        failed.append("negative_leak_rate")
    if failed:
        raise SystemExit(f"semantic gates failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
