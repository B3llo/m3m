"""Synthetic benchmark test for agent-m3m scale performance."""

import time
import tempfile
import random
from pathlib import Path

from agent_m3m.models import MemoryItem, MemoryScope, MemoryType, MemoryStatus, generate_memory_id
from agent_m3m.config import Config
from agent_m3m.store import MemoryStore
from agent_m3m.retriever import MemoryRetriever


SAMPLE_TAGS = ["docker", "terraform", "node", "python", "pnpm", "react", "postgres", "redis", "gcp", "git"]
SAMPLE_ENTITIES = ["pnpm", "docker", "terraform", "fastapi", "nextjs", "kubernetes", "vitest", "pytest"]

SAMPLE_TOPICS = [
    "Prefers local container development over remote staging servers.",
    "Always run pnpm test before pushing commits to main branch.",
    "Use modular build structure with remote cache locks.",
    "FastAPI endpoints should return typed Pydantic models.",
    "Avoid running Node processes as root inside Docker containers.",
    "PostgreSQL indexes should be created concurrently for production migrations.",
    "Use Redis for session caching with 24-hour expiration TTL.",
    "React components must be wrapped in React.memo when rendering long lists.",
    "Always specify python-version 3.11 in GitHub Actions workflows.",
    "Use vitest instead of jest for unit testing modern TypeScript packages.",
]


def run_benchmark(count: int):
    with tempfile.TemporaryDirectory() as tmp_dir:
        global_path = Path(tmp_dir) / "global_memory"
        project_path = Path(tmp_dir) / "project_memory"
        config = Config(global_path=global_path, project_path=project_path)
        store = MemoryStore(config)
        retriever = MemoryRetriever(store, config)

        store.init_global()

        print(f"--- Generating {count} synthetic memories ---")
        t0 = time.time()
        for i in range(count):
            topic = random.choice(SAMPLE_TOPICS)
            item = MemoryItem(
                id=f"mem_bench_{i:05d}",
                content=f"{topic} (Ref ID: {i})",
                scope=MemoryScope.GLOBAL,
                type=random.choice(list(MemoryType)),
                tags=random.sample(SAMPLE_TAGS, k=2),
                entities=random.sample(SAMPLE_ENTITIES, k=1),
            )
            store.save_item(item)
        t_gen = time.time() - t0
        print(f"✓ Generated and written to disk in {t_gen:.3f}s")

        print(f"--- Indexing {count} memories into SQLite FTS5 ---")
        t0 = time.time()
        indexed = retriever.reindex()
        t_index = time.time() - t0
        db_size_kb = retriever.db_path.stat().st_size / 1024
        print(f"✓ Indexed {indexed} items in {t_index:.3f}s (FTS5 DB size: {db_size_kb:.1f} KB)")

        print("--- Query Performance ---")
        queries = ["pnpm test", "docker container", "build module", "fastapi pydantic", "redis cache"]
        latencies = []
        for q in queries:
            t0 = time.time()
            results = retriever.search(q, top_k=5)
            dt = (time.time() - t0) * 1000
            latencies.append(dt)
            print(f"  Search '{q}': {len(results)} hits in {dt:.2f} ms")

        avg_lat = sum(latencies) / len(latencies)
        print(f"✓ Average search latency: {avg_lat:.2f} ms\n")


if __name__ == "__main__":
    print("=== AGENT-MEMORY PERFORMANCE BENCHMARK ===")
    run_benchmark(100)
    run_benchmark(1000)
