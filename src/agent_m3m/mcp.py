"""Minimal MCP (Model Context Protocol) stdio server for agent-m3m.

Zero-dependency implementation: newline-delimited JSON-RPC 2.0 over stdio.
Exposes the local Markdown memory store to any MCP client (Claude Code,
Codex, Cursor, Claude Desktop, ...), keeping .md files as the only source
of truth. Tools carry MCP behavior annotations so agents can pick them
without trial and error.
"""

import json
import sys
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .context import ContextBuilder
from .models import MemoryScope, MemoryStatus
from .retriever import MemoryRetriever
from .sanitizer import scrub_secrets
from .store import MemoryStore

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "agent-m3m", "version": "0.3.0"}
MAX_LINE_CHARS = 10_000_000

_READ_ONLY = {"readOnlyHint": True}
_WRITE = {"readOnlyHint": False, "destructiveHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "m3m_context",
        "description": (
            "Build the bounded persistent-memory context (core memory, pinned "
            "memories, prompt-relevant archival memories and recent learnings)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "Current task or prompt for targeted retrieval"}},
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "m3m_search",
        "description": "Search durable memories with local FTS5 (BM25 + entity/tag/usage ranking).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
            },
            "required": ["query"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "m3m_show",
        "description": "Show the full Markdown source of one memory by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "m3m_remember",
        "description": (
            "Persist one durable, self-contained memory (convention, decision, "
            "lesson, workflow or preference) as Markdown. Secrets are scrubbed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Self-contained memory text"},
                "scope": {"type": "string", "enum": ["global", "project"], "default": "project"},
                "type": {
                    "type": "string",
                    "enum": ["preference", "lesson", "decision", "convention", "tool", "project_fact", "workflow"],
                    "default": "convention",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "m3m_pin",
        "description": (
            "Pin a memory so it is always injected into future session contexts, "
            "or unpin it to remove that guarantee."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "unpin": {"type": "boolean", "default": False}},
            "required": ["id"],
        },
        # The same tool can unpin a memory, which removes it from guaranteed
        # future context injection. Do not advertise the combined operation as
        # non-destructive to MCP clients.
        "annotations": _DESTRUCTIVE,
    },
    {
        "name": "m3m_forget",
        "description": "Archive a memory by ID (history is kept; use m3m restore in the CLI to bring it back).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "annotations": _DESTRUCTIVE,
    },
]


class _ServerContext:
    def __init__(self) -> None:
        self.config = Config()
        self.store = MemoryStore(self.config)
        self.retriever = MemoryRetriever(self.store, self.config)
        self.builder = ContextBuilder(self.store, self.retriever, self.config)


def _tool_context(args: Dict[str, Any], ctx: _ServerContext) -> str:
    return ctx.builder.build_context(prompt=args.get("prompt"), record_usage=False)


def _tool_search(args: Dict[str, Any], ctx: _ServerContext) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Empty query."
    limit = args.get("limit") or 5
    try:
        limit = max(1, min(25, int(limit)))
    except (TypeError, ValueError):
        limit = 5
    results = ctx.retriever.search(query, top_k=limit, status="active")
    if not results:
        return f"No memories found matching '{scrub_secrets(query)[:200]}'."
    lines = [f"Found {len(results)} matching memories:"]
    for index, candidate in enumerate(results, 1):
        item = candidate.item
        lines.append(f"{index}. [{candidate.total_score:.2f}] {item.id} ({item.scope.value}/{item.type.value})")
        lines.append(f"   {item.content}")
    return "\n".join(lines)


def _tool_show(args: Dict[str, Any], ctx: _ServerContext) -> str:
    item = ctx.store.load_item_by_id(str(args.get("id", "")))
    return item.to_markdown() if item else f"Memory with ID '{scrub_secrets(str(args.get('id', '')))[:100]}' not found."


def _tool_remember(args: Dict[str, Any], ctx: _ServerContext) -> str:
    # Imported lazily: cli constructs the argparse parser only on demand, and
    # this avoids the circular import at module load time.
    from .cli import _remember_memory

    scope = MemoryScope(str(args.get("scope", "project")))
    mtype = _safe_memory_type(str(args.get("type", "convention")))
    tags = [str(tag) for tag in (args.get("tags") or []) if str(tag).strip()][:10]
    saved, duplicate = _remember_memory(
        ctx.config,
        ctx.store,
        ctx.retriever,
        str(args.get("text", "")),
        scope,
        mtype,
        tags,
        [],
        None,
        False,
    )
    if duplicate:
        return f"Memory already exists: {duplicate.id}"
    return f"Remembered {saved.id} ({saved.scope.value}/{saved.type.value}/{saved.status.value})"


def _tool_pin(args: Dict[str, Any], ctx: _ServerContext) -> str:
    item = ctx.store.load_item_by_id(str(args.get("id", "")))
    if not item:
        return f"Memory with ID '{scrub_secrets(str(args.get('id', '')))[:100]}' not found."
    item.pinned = not bool(args.get("unpin", False))
    ctx.store.save_item(item)
    ctx.retriever.reindex()
    return f"Memory '{item.id}' {'pinned' if item.pinned else 'unpinned'}."


def _tool_forget(args: Dict[str, Any], ctx: _ServerContext) -> str:
    item_id = str(args.get("id", ""))
    if ctx.store.update_status(item_id, MemoryStatus.ARCHIVED):
        ctx.retriever.reindex()
        return f"Memory '{item_id}' set to archived."
    return f"Memory with ID '{scrub_secrets(item_id)[:100]}' not found."


def _safe_memory_type(value: str):
    from .models import MemoryType

    try:
        return MemoryType(value)
    except ValueError:
        return MemoryType.CONVENTION


TOOL_HANDLERS = {
    "m3m_context": _tool_context,
    "m3m_search": _tool_search,
    "m3m_show": _tool_show,
    "m3m_remember": _tool_remember,
    "m3m_pin": _tool_pin,
    "m3m_forget": _tool_forget,
}


def _handle_message(message: Any, ctx: _ServerContext) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return None
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                # This server implements one protocol revision. Reporting an
                # arbitrary client value would falsely claim support for it.
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = str(params.get("name", ""))
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": f"Unknown tool: {scrub_secrets(name)[:100]}"},
            }
        try:
            text = handler(arguments, ctx)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except Exception as exc:  # fail-open: tool errors must not kill the server
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": scrub_secrets(str(exc))[:500]}],
                    "isError": True,
                },
            }
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def run_mcp_server(stdin=None, stdout=None) -> None:
    """Serve the Markdown memory store over MCP stdio until stdin closes."""
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    ctx = _ServerContext()
    for line in input_stream:
        line = line.strip()
        if not line or len(line) > MAX_LINE_CHARS:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        response = _handle_message(message, ctx)
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()


if __name__ == "__main__":
    run_mcp_server()
