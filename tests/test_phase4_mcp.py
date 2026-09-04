"""Phase 4 tests: minimal MCP stdio server over the Markdown store."""

import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from agent_m3m.config import Config
from agent_m3m.mcp import run_mcp_server
from agent_m3m.store import MemoryStore


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = Config(
            global_path=root / "global_memory",
            project_path=root / "project_memory",
        )
        self.store = MemoryStore(self.config)
        self.store.init_global()
        self.store.init_project()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _serve(self, messages):
        stdin = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n")
        stdout = io.StringIO()
        with unittest.mock.patch("agent_m3m.mcp._ServerContext", return_value=self._context()):
            run_mcp_server(stdin=stdin, stdout=stdout)
        return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    def _context(self):
        from agent_m3m.mcp import _ServerContext

        context = _ServerContext.__new__(_ServerContext)
        context.config = self.config
        context.store = self.store
        from agent_m3m.retriever import MemoryRetriever

        context.retriever = MemoryRetriever(self.store, self.config)
        from agent_m3m.context import ContextBuilder

        context.builder = ContextBuilder(self.store, context.retriever, self.config)
        return context

    def test_initialize_echoes_protocol_and_lists_tools(self):
        responses = self._serve([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])

        self.assertEqual(len(responses), 2)  # notification never responds
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "agent-m3m")
        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertIn("m3m_context", tool_names)
        self.assertIn("m3m_remember", tool_names)
        self.assertIn("m3m_search", tool_names)
        forget = next(tool for tool in responses[1]["result"]["tools"] if tool["name"] == "m3m_forget")
        self.assertTrue(forget["annotations"]["destructiveHint"])

    def test_remember_then_search_via_tools(self):
        responses = self._serve([
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "m3m_remember", "arguments": {
                    "text": "Always run pytest -m unit before commits.",
                    "scope": "project", "type": "workflow", "tags": ["tests"],
                }},
            },
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "m3m_search", "arguments": {"query": "pytest unit commits"}},
            },
        ])

        remember_text = responses[0]["result"]["content"][0]["text"]
        self.assertIn("Remembered", remember_text)
        self.assertEqual(self.store.stats()["total"], 1)

        search_text = responses[1]["result"]["content"][0]["text"]
        self.assertIn("pytest -m unit", search_text)

    def test_duplicate_remember_reports_existing_id(self):
        responses = self._serve([
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "m3m_remember", "arguments": {"text": "Deploy uses scenario-specific release plans."}},
            },
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "m3m_remember", "arguments": {"text": "Deploy uses scenario-specific release plans."}},
            },
        ])

        self.assertIn("already exists", responses[1]["result"]["content"][0]["text"])

    def test_forget_archives_and_context_tool_returns_text(self):
        from agent_m3m.models import MemoryItem, MemoryScope, MemoryStatus

        self.store.save_item(MemoryItem(
            id="mem_mcp", content="Use the local SQLite migration workflow.",
            scope=MemoryScope.PROJECT, project_id=self.config.project_id,
            status=MemoryStatus.ACTIVE,
        ))
        responses = self._serve([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "m3m_forget", "arguments": {"id": "mem_mcp"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "m3m_context", "arguments": {"prompt": "SQLite migrations"}}},
        ])

        self.assertIn("archived", responses[0]["result"]["content"][0]["text"])
        self.assertEqual(self.store.load_item_by_id("mem_mcp").status, MemoryStatus.ARCHIVED)
        self.assertIn("AGENT PERSISTENT MEMORY CONTEXT", responses[1]["result"]["content"][0]["text"])

    def test_unknown_tool_returns_jsonrpc_error(self):
        responses = self._serve([
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        ])
        self.assertEqual(responses[0]["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
