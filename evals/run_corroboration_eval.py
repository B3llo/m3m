#!/usr/bin/env python3
"""Run the deterministic corroboration-reconcile evaluation.

For each keeper/candidate pair in corroboration_cases.json, runs
MemoryExtractor._reconcile_with_existing with a scripted LLM (always ADD)
and reports the decision path per family:

- skipped_silent: SKIP without any LLM call (exact, >=0.80, or auto band)
- llm_consulted: the bounded reconcile call ran (0.30-0.80 band)
- added: default ADD path

Adversarial families (negation, numbers, instead) must NEVER be
silently skipped. Exit code honors --min-adversarial-safe.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_m3m.config import Config
from agent_m3m.extractor import MemoryExtractor
from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus
from agent_m3m.textutils import jaccard, meaningful_tokens

try:
    from agent_m3m.textutils import contradiction_guard_tripped
except ImportError:
    contradiction_guard_tripped = None

DEFAULT_CASES = Path(__file__).with_name("corroboration_cases.json")
ADVERSARIAL = {"negation", "numbers", "instead"}


class ScriptedRunner:
    """LLM stub that always answers ADD and counts invocations."""

    def __init__(self):
        self.calls = 0

    def run_prompt(self, prompt, provider=None, output_schema=None):
        self.calls += 1
        return True, '{"decisions": [{"candidate": 0, "action": "ADD", "id": null}]}'


def run_eval(cases_path=DEFAULT_CASES):
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("cases"), list):
        raise ValueError("Unsupported corroboration evaluation schema")
    cases = payload["cases"]
    if not cases:
        raise ValueError("Corroboration evaluation has no cases")

    with tempfile.TemporaryDirectory(prefix="m3m-corrob-eval-") as temp_dir:
        root = Path(temp_dir)
        config = Config(global_path=root / "global", project_path=root / "project" / ".agent-m3m")
        rows = []
        for case in cases:
            runner = ScriptedRunner()
            extractor = MemoryExtractor(config=config, llm_runner=runner)
            keeper = MemoryItem(
                id=f"keeper_{case['id']}",
                content=case["keeper"],
                scope=MemoryScope.PROJECT,
                project_id=config.project_id,
                status=MemoryStatus.ACTIVE,
            )
            candidate = MemoryItem(
                id=f"cand_{case['id']}",
                content=case["candidate"],
                scope=MemoryScope.PROJECT,
                project_id=config.project_id,
                status=MemoryStatus.ACTIVE,
            )
            score = jaccard(meaningful_tokens(keeper.content), meaningful_tokens(candidate.content))
            decisions = extractor._reconcile_with_existing([candidate], [keeper])
            action = decisions[0].get("action", "ADD")
            skipped_silent = action == "SKIP" and runner.calls == 0
            rows.append({
                "id": case["id"],
                "family": case["family"],
                "jaccard": round(score, 3),
                "guard_tripped": bool(contradiction_guard_tripped(keeper.content, candidate.content))
                if contradiction_guard_tripped
                else None,
                "action": action,
                "llm_calls": runner.calls,
                "skipped_silent": skipped_silent,
            })

    families = {}
    for row in rows:
        fam = families.setdefault(row["family"], {"cases": 0, "skipped_silent": 0, "llm_consulted": 0})
        fam["cases"] += 1
        fam["skipped_silent"] += int(row["skipped_silent"])
        fam["llm_consulted"] += int(row["llm_calls"] > 0)

    adversarial_rows = [r for r in rows if r["family"] in ADVERSARIAL]
    adversarial_safe = (
        sum(1 for r in adversarial_rows if not r["skipped_silent"]) / len(adversarial_rows)
        if adversarial_rows
        else 1.0
    )
    close_rows = [r for r in rows if r["family"] == "paraphrase_close"]
    close_skip = (
        sum(1 for r in close_rows if r["skipped_silent"] and r["llm_calls"] == 0) / len(close_rows)
        if close_rows
        else 1.0
    )
    identical_rows = [r for r in rows if r["family"] == "identical"]
    identical_skip = (
        sum(1 for r in identical_rows if r["skipped_silent"] and r["llm_calls"] == 0) / len(identical_rows)
        if identical_rows
        else 1.0
    )
    loose_rows = [r for r in rows if r["family"] in ("paraphrase_loose", "distinct")]
    loose_safe = (
        sum(1 for r in loose_rows if not r["skipped_silent"]) / len(loose_rows)
        if loose_rows
        else 1.0
    )
    return {
        "schema_version": 1,
        "cases": len(rows),
        "families": families,
        "adversarial_safe_rate": adversarial_safe,
        "close_skip_rate": close_skip,
        "identical_skip_rate": identical_skip,
        "loose_safe_rate": loose_safe,
        "total_llm_calls": sum(r["llm_calls"] for r in rows),
        "results": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--min-adversarial-safe", type=float, default=1.0)
    parser.add_argument("--min-close-skip", type=float, default=1.0)
    parser.add_argument("--min-identical-skip", type=float, default=1.0)
    parser.add_argument("--min-loose-safe", type=float, default=1.0)
    args = parser.parse_args()
    report = run_eval(args.cases)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    failed = []
    if report["adversarial_safe_rate"] < args.min_adversarial_safe:
        failed.append("adversarial_safe_rate")
    if report["close_skip_rate"] < args.min_close_skip:
        failed.append("close_skip_rate")
    if report["identical_skip_rate"] < args.min_identical_skip:
        failed.append("identical_skip_rate")
    if report["loose_safe_rate"] < args.min_loose_safe:
        failed.append("loose_safe_rate")
    if failed:
        raise SystemExit(f"corroboration gates failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
