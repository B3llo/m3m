# Architecture

agent-m3m is a preview-environment pipeline with Markdown as the durable source of truth. SQLite FTS5, topic pages, universal exports, queue state, and diagnostics are derived or operational data and can be rebuilt independently of the memory files.

## Data flow

1. Agent lifecycle hooks pass a prompt or completed transcript to the CLI.
2. Session-end capture writes a bounded job to the project-local durable queue and returns protocol JSON immediately.
3. A single-flight worker extracts candidate memories, scrubs known secret formats, reconciles duplicates and contradictions, then writes Markdown.
4. The FTS5 index is rebuilt or incrementally refreshed from Markdown.
5. Context building combines bounded core rules with query-relevant retrieval. Normal CLI context records rate-limited usage feedback; MCP read-only context does not mutate memory files.

## Components

- `config.py`: configuration loading, validation, and private atomic writes.
- `store.py` and `models.py`: Markdown persistence and schema compatibility.
- `retriever.py`: disposable FTS5 index and ranking.
- `extractor.py`: provider invocation, extraction, and reconciliation.
- `queue.py`: durable session capture and single-flight processing.
- `context.py` and `handoff.py`: bounded context assembly and session continuity.
- `hooks_installer.py`: selective, idempotent integration management.
- `mcp.py`: zero-dependency MCP stdio adapter.

The CLI currently coordinates these modules in one process. Large CLI modules are known refactoring candidates, but public storage and hook protocols should be separated behind application services before any broad reorganization.
