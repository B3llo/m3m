"""CLI Interface for agent-memory."""

import argparse
import sys
import json
from pathlib import Path
from typing import Optional

from .config import Config
from .store import MemoryStore
from .retriever import MemoryRetriever
from .extractor import MemoryExtractor
from .context import ContextBuilder
from .doctor import Doctor
from .models import MemoryScope, MemoryStatus


def main():
    parser = argparse.ArgumentParser(
        prog="agent-memory",
        description="Local-first persistent memory for AI coding agents with Markdown as Source of Truth",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize memory directory structure")
    init_parser.add_argument("--global", dest="is_global", action="store_true", help="Initialize global ~/.agent-memory")

    # context
    context_parser = subparsers.add_parser("context", help="Output Core Memory context for agent session start")

    # search
    search_parser = subparsers.add_parser("search", help="Search memories using local FTS5 index")
    search_parser.add_argument("query", type=str, help="Search query string")
    search_parser.add_argument("--limit", type=int, default=5, help="Max candidates to return")
    search_parser.add_argument("--debug", action="store_true", help="Show detailed score breakdown")
    search_parser.add_argument("--json", action="store_true", help="Output JSON format")

    # save
    save_parser = subparsers.add_parser("save", help="Extract and save memories from session transcript")
    save_parser.add_argument("--transcript-file", type=str, help="Path to transcript text file")
    save_parser.add_argument("--stdin", action="store_true", help="Read transcript from stdin")
    save_parser.add_argument("--provider", type=str, help="Override LLM provider for extraction")

    # list
    list_parser = subparsers.add_parser("list", help="List all stored memories")
    list_parser.add_argument("--scope", choices=["global", "project"], help="Filter by scope")
    list_parser.add_argument("--status", choices=["active", "superseded", "archived"], default="active", help="Filter by status")

    # show
    show_parser = subparsers.add_parser("show", help="Show full content of a memory by ID")
    show_parser.add_argument("id", type=str, help="Memory ID")

    # forget
    forget_parser = subparsers.add_parser("forget", help="Archive/forget a memory by ID")
    forget_parser.add_argument("id", type=str, help="Memory ID")

    # reindex
    reindex_parser = subparsers.add_parser("reindex", help="Rebuild SQLite FTS5 index from Markdown SSOT")

    # clean
    clean_parser = subparsers.add_parser("clean", help="Validate memories and rebuild index")

    # doctor
    doctor_parser = subparsers.add_parser("doctor", help="Run system diagnostics")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = Config()
    store = MemoryStore(config)
    retriever = MemoryRetriever(store, config)

    if args.command == "init":
        if getattr(args, "is_global", False):
            path = store.init_global()
            print(f"✓ Initialized global memory at {path}")
        else:
            path = store.init_project()
            print(f"✓ Initialized project memory at {path}")

    elif args.command == "context":
        builder = ContextBuilder(store, retriever, config)
        print(builder.build_context())

    elif args.command == "search":
        candidates = retriever.search(
            query=args.query,
            top_k=args.limit,
            debug=args.debug,
        )

        if args.json:
            output = []
            for c in candidates:
                output.append({
                    "id": c.item.id,
                    "content": c.item.content,
                    "score": c.total_score,
                    "scope": c.item.scope,
                    "type": c.item.type,
                    "status": c.item.status,
                    "breakdown": c.breakdown if args.debug else None,
                })
            print(json.dumps(output, indent=2))
        else:
            if not candidates:
                print(f"No memories found matching '{args.query}'")
                return

            print(f"Found {len(candidates)} matching memories:\n")
            for i, c in enumerate(candidates, 1):
                item = c.item
                print(f"{i}. [{c.total_score:.2f}] {item.id} ({item.scope}/{item.type})")
                print(f"   {item.content}")
                if args.debug:
                    print(f"   Breakdown: {c.breakdown}")
                print()

    elif args.command == "save":
        transcript = ""
        if args.transcript_file:
            path = Path(args.transcript_file)
            if path.exists():
                transcript = path.read_text(encoding="utf-8")
        elif args.stdin or not sys.stdin.isatty():
            transcript = sys.stdin.read()

        if not transcript.strip():
            print("No transcript content provided for extraction.", file=sys.stderr)
            sys.exit(1)

        extractor = MemoryExtractor(store, retriever, llm_runner=None, config=config)
        saved = extractor.extract_from_transcript(transcript, provider=args.provider)
        print(f"✓ Saved {len(saved)} new memories.")
        for item in saved:
            print(f"  - {item.id}: {item.content[:70]}...")

    elif args.command == "list":
        scope_enum = MemoryScope(args.scope) if args.scope else None
        status_enum = MemoryStatus(args.status) if args.status else MemoryStatus.ACTIVE

        items = store.list_items(scope=scope_enum, status=status_enum)
        print(f"Stored memories ({len(items)} items):\n")
        for item in items:
            print(f"- {item.id} [{item.scope}/{item.type} - {item.status}]")
            print(f"  {item.content[:80]}")

    elif args.command == "show":
        item = store.load_item_by_id(args.id)
        if item:
            print(item.to_markdown())
        else:
            print(f"Memory with ID '{args.id}' not found.", file=sys.stderr)

    elif args.command == "forget":
        success = store.update_status(args.id, MemoryStatus.ARCHIVED)
        if success:
            retriever.reindex()
            print(f"✓ Memory '{args.id}' set to archived.")
        else:
            print(f"Memory with ID '{args.id}' not found.", file=sys.stderr)

    elif args.command == "reindex" or args.command == "clean":
        count = retriever.reindex()
        print(f"✓ Successfully rebuilt FTS5 index with {count} memories.")

    elif args.command == "doctor":
        doc = Doctor(config)
        results = doc.run_diagnostics()
        print("=== agent-memory doctor ===\n")
        all_ok = True
        for name, msg, ok in results:
            symbol = "✓" if ok else "✗"
            if not ok:
                all_ok = False
            print(f"[{symbol}] {name}: {msg}")
        print()
        if all_ok:
            print("System status: HEALTHY")
        else:
            print("System status: ATTENTION NEEDED")


if __name__ == "__main__":
    main()
