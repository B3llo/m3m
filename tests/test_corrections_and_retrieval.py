"""Tests covering user corrections, shortcuts, long prompt retrieval, and isolation."""

import json
import tempfile
import unittest
from pathlib import Path

from agent_m3m.config import Config
from agent_m3m.context import ContextBuilder
from agent_m3m.doctor import Doctor
from agent_m3m.extractor import MemoryExtractor
from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus, MemoryType, generate_memory_id
from agent_m3m.retriever import MemoryRetriever
from agent_m3m.store import MemoryStore


class StubRunner:
    def __init__(self, response: str, success: bool = True):
        self.response = response
        self.success = success

    def run_prompt(self, prompt, **kwargs):
        return self.success, self.response


class TestCorrectionsAndRetrieval(unittest.TestCase):
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

    def test_extract_user_corrections_and_shortcuts_via_llm(self):
        llm_response = json.dumps({
            "memories": [
                {
                    "content": "Não use pytest diretamente; execute testes sempre com pytest -m unit",
                    "scope": "project",
                    "type": "workflow",
                    "confidence": 0.98,
                    "tags": ["testing", "pytest", "shortcut"],
                    "entities": ["pytest"],
                },
                {
                    "content": "Ignore a biblioteca standard unittest e use pytest nos novos testes",
                    "scope": "project",
                    "type": "convention",
                    "confidence": 0.95,
                    "tags": ["convention", "testing"],
                    "entities": ["pytest", "unittest"],
                },
                {
                    "content": "O agente tentou alterar o schema sem migration; sempre crie arquivo de migração primeiro",
                    "scope": "project",
                    "type": "lesson",
                    "confidence": 0.92,
                    "tags": ["migration", "schema", "lesson"],
                    "entities": ["alembic"],
                },
            ]
        })
        extractor = MemoryExtractor(
            self.store,
            self.retriever,
            llm_runner=StubRunner(llm_response),
            config=self.config,
        )
        transcript = (
            "USER: Não faça assim! Ignore o unittest e use pytest.\n"
            "ASSISTANT: Entendido.\n"
            "USER: Para rodar testes, use o atalho pytest -m unit.\n"
            "ASSISTANT: Executando os testes...\n"
            "USER: Você errou ao tentar mexer no schema sem migration. Crie migration primeiro."
        )
        saved = extractor.extract_from_transcript(transcript, source="manual-save")
        self.assertEqual(len(saved), 3)

        # Check types
        types = {item.type for item in saved}
        self.assertIn(MemoryType.WORKFLOW, types)
        self.assertIn(MemoryType.CONVENTION, types)
        self.assertIn(MemoryType.LESSON, types)

        # Check statuses are active
        for item in saved:
            self.assertEqual(item.status, MemoryStatus.ACTIVE)

    def test_heuristic_fallback_extracts_corrections_and_shortcuts(self):
        extractor = MemoryExtractor(
            self.store,
            self.retriever,
            llm_runner=StubRunner("Invalid JSON error", success=False),
            config=self.config,
        )
        transcript = (
            "USER: Não faça build com webpack; em vez de webpack use vite para buildar o frontend.\n"
            "USER: Para testar use pytest -m unit.\n"
            "USER: Lembre de nunca usar print em produção.\n"
            "USER: Decisão: adotamos FastAPI para todos os endpoints."
        )
        saved = extractor.extract_from_transcript(transcript, source="manual-save")
        self.assertGreaterEqual(len(saved), 3)
        contents = " ".join(item.content for item in saved)
        self.assertTrue(
            "vite" in contents or "pytest" in contents or "FastAPI" in contents
        )

    def test_long_prompt_retrieval(self):
        # Save custom shortcut memory
        item = MemoryItem(
            id=generate_memory_id(),
            content="Para rodar a suite de testes use o comando pytest -m unit --disable-warnings",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            type=MemoryType.WORKFLOW,
            status=MemoryStatus.ACTIVE,
            confidence=1.0,
            tags=["pytest", "unit", "testing"],
            entities=["pytest"],
        )
        self.store.save_item(item)
        self.retriever.reindex()

        # Search with a long, verbose user prompt in Portuguese
        long_prompt = (
            "Olá agente, por favor gostaria que você fizesse a execução da suite de testes "
            "do nosso projeto python para verificar se algo quebrou nas mudanças recentes."
        )
        candidates = self.retriever.search(long_prompt, top_k=5)
        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0].item.id, item.id)

    def test_context_builder_includes_relevant_archival_memories_for_prompt(self):
        item = MemoryItem(
            id=generate_memory_id(),
            content="Não use mock no banco de dados SQLite; use o arquivo temporário",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            type=MemoryType.CONVENTION,
            status=MemoryStatus.ACTIVE,
            confidence=1.0,
            tags=["sqlite", "database", "testing"],
            entities=["sqlite"],
        )
        self.store.save_item(item)
        self.retriever.reindex()

        builder = ContextBuilder(self.store, self.retriever, self.config)
        prompt = "Preciso adicionar testes para a persistência no banco SQLite."
        context = builder.build_context(prompt=prompt)
        self.assertIn("Não use mock no banco de dados SQLite", context)

    def test_project_index_isolation(self):
        # Memory in project A
        item_a = MemoryItem(
            id=generate_memory_id(),
            content="Use secret token alpha-one for authorization",
            scope=MemoryScope.PROJECT,
            project_id="project-alpha-123",
            type=MemoryType.CONVENTION,
            status=MemoryStatus.ACTIVE,
            confidence=1.0,
            tags=["auth"],
        )
        self.store.save_item(item_a)

        # Config pointing to project B
        config_b = Config(
            global_path=self.global_path,
            project_path=Path(self.tmp_dir.name) / "project_b_mem",
        )
        store_b = MemoryStore(config_b)
        store_b.init_project()
        retriever_b = MemoryRetriever(store_b, config_b)
        retriever_b.reindex()

        # Search from project B should NOT return project A's isolated memory
        results = retriever_b.search("alpha-one")
        self.assertEqual(len(results), 0)

    def test_retriever_deduplication(self):
        # Save two items with the same normalized content
        item1 = MemoryItem(
            id=generate_memory_id(),
            content="Always use ruff for linting python code",
            scope=MemoryScope.GLOBAL,
            type=MemoryType.CONVENTION,
            status=MemoryStatus.ACTIVE,
            confidence=1.0,
            tags=["ruff", "lint"],
        )
        item2 = MemoryItem(
            id=generate_memory_id(),
            content="always use ruff for linting python code",
            scope=MemoryScope.PROJECT,
            project_id=self.config.project_id,
            type=MemoryType.CONVENTION,
            status=MemoryStatus.ACTIVE,
            confidence=0.9,
            tags=["ruff"],
        )
        self.store.save_item(item1)
        self.store.save_item(item2)
        self.retriever.reindex()

        results = self.retriever.search("ruff linting")
        self.assertEqual(len(results), 1)

    def test_doctor_with_no_resource_warnings(self):
        doctor = Doctor(self.config)
        results = doctor.run_diagnostics(verify_provider=False)
        self.assertTrue(all(isinstance(r, tuple) and len(r) == 3 for r in results))
