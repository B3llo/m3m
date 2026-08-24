"""Unit tests for agent-memory."""

import tempfile
import unittest
from pathlib import Path

from agent_memory.models import MemoryItem, MemoryScope, MemoryType, MemoryStatus, generate_memory_id
from agent_memory.sanitizer import scrub_secrets, has_secrets
from agent_memory.config import Config
from agent_memory.store import MemoryStore
from agent_memory.retriever import MemoryRetriever
from agent_memory.doctor import Doctor


class TestAgentMemory(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.global_path = Path(self.tmp_dir.name) / "global_memory"
        self.proj_path = Path(self.tmp_dir.name) / "project_memory"

        self.config = Config(global_path=self.global_path, project_path=self.proj_path)
        self.store = MemoryStore(self.config)
        self.retriever = MemoryRetriever(self.store, self.config)

        self.store.init_global()
        self.store.init_project()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_secret_sanitizer(self):
        secret_text = "My secret key is sk-proj-1234567890abcdef1234567890abcdef and token ghp_1234567890abcdef1234567890abcdef"
        self.assertTrue(has_secrets(secret_text))

        clean_text = scrub_secrets(secret_text)
        self.assertNotIn("sk-proj-1234567890abcdef", clean_text)
        self.assertIn("[REDACTED_API_KEY]", clean_text)
        self.assertIn("[REDACTED_GITHUB_TOKEN]", clean_text)

    def test_memory_item_frontmatter_roundtrip(self):
        item = MemoryItem(
            id="mem_test_123",
            content="Prefers orbitpm over npm for Node.js projects.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.PREFERENCE,
            status=MemoryStatus.ACTIVE,
            confidence=0.95,
            tags=["javascript", "orbitpm"],
            entities=["orbitpm", "npm"],
        )
        md = item.to_markdown()
        self.assertIn('id: "mem_test_123"', md)
        self.assertIn('scope: "global"', md)

        reloaded = MemoryItem.from_markdown(md)
        self.assertEqual(reloaded.id, "mem_test_123")
        self.assertEqual(reloaded.content, "Prefers orbitpm over npm for Node.js projects.")
        self.assertEqual(reloaded.tags, ["javascript", "orbitpm"])
        self.assertEqual(reloaded.entities, ["orbitpm", "npm"])

    def test_store_crud(self):
        item1 = MemoryItem(
            id=generate_memory_id(),
            content="Use StackSpec modules with local state.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.LESSON,
        )
        saved = self.store.save_item(item1)
        self.assertTrue(Path(saved.file_path).exists())

        loaded = self.store.load_item_by_id(saved.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.content, "Use StackSpec modules with local state.")

    def test_fts5_indexing_and_ranking(self):
        item1 = MemoryItem(
            id="mem_01",
            content="Use orbitpm package manager in Node projects.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.PREFERENCE,
            tags=["orbitpm", "node"],
            entities=["orbitpm"],
        )
        item2 = MemoryItem(
            id="mem_02",
            content="Docker containers should run as non-root user.",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.LESSON,
            tags=["docker", "security"],
            entities=["docker"],
        )
        self.store.save_item(item1)
        self.store.save_item(item2)

        self.retriever.reindex()

        results = self.retriever.search("orbitpm node", top_k=5, debug=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].item.id, "mem_01")
        self.assertIn("lexical", results[0].breakdown)

    def test_superseded_status(self):
        old_item = MemoryItem(
            id="mem_old",
            content="Prefers npm package manager.",
            status=MemoryStatus.ACTIVE,
        )
        self.store.save_item(old_item)

        new_item = MemoryItem(
            id="mem_new",
            content="Prefers orbitpm package manager.",
            status=MemoryStatus.ACTIVE,
            superseded_by="mem_old",
        )

        # Update old item status
        self.store.update_status("mem_old", MemoryStatus.SUPERSEDED, superseded_by="mem_new")

        old_reloaded = self.store.load_item_by_id("mem_old")
        self.assertEqual(old_reloaded.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(old_reloaded.superseded_by, "mem_new")

    def test_doctor_diagnostics(self):
        doc = Doctor(self.config)
        diag = doc.run_diagnostics()
        self.assertTrue(len(diag) >= 5)


if __name__ == "__main__":
    unittest.main()
