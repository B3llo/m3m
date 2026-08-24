"""CLI Interface for agent-m3m."""

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
from .hooks_installer import HooksInstaller
from .models import MemoryScope, MemoryStatus


def run_interactive_setup(config: Config):
    """Interactive wizard to configure LLM provider, model, and effort level."""
    print("\n=== m3m Configuration Setup ===")
    print("Select LLM Provider for background memory extraction:")
    print("  1) OpenAI Codex CLI (Uses your existing Codex subscription/plan) [Default]")
    print("  2) Claude Code CLI (Uses your existing Claude Code setup)")
    print("  3) OpenCode CLI")
    print("  4) Kimi CLI")
    
    choice = input("Select provider [1-4, Default: 1]: ").strip()
    if choice == "2":
        config.active_provider = "claude"
        default_model = "haiku"
    elif choice == "3":
        config.active_provider = "opencode"
        default_model = "default"
    elif choice == "4":
        config.active_provider = "kimi"
        default_model = "default"
    else:
        config.active_provider = "codex"
        default_model = "luna"

    model_input = input(f"Model name for background memory extraction [Default: {default_model}]: ").strip()
    config.model_name = model_input if model_input else default_model

    if config.active_provider == "codex":
        effort_input = input("Reasoning effort for Codex (low/medium/high) [Default: low]: ").strip().lower()
        if effort_input in ["low", "medium", "high"]:
            config.reasoning_effort = effort_input
        else:
            config.reasoning_effort = "low"

    config.save_config()
    print(f"\n✓ Configuration saved to {config.config_file}")
    print(f"  - Active Provider: {config.active_provider}")
    print(f"  - Model: {config.model_name}")
    print(f"  - Effort: {config.reasoning_effort}\n")


def main():
    parser = argparse.ArgumentParser(
        prog="m3m",
        description="Local-first persistent memory for AI coding agents with Markdown as Source of Truth",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize memory directory structure and wire hooks")
    init_parser.add_argument("--global", dest="is_global", action="store_true", help="Initialize global ~/.agent-m3m")
    init_parser.add_argument("--provider", choices=["codex", "claude", "opencode", "kimi"], help="Active LLM provider")
    init_parser.add_argument("--model", type=str, help="LLM model name (e.g. luna, haiku)")
    init_parser.add_argument("--effort", choices=["low", "medium", "high"], help="Reasoning effort level")
    init_parser.add_argument("--non-interactive", action="store_true", help="Skip interactive prompt wizard")

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
        # Interactive configuration if interactive shell
        if sys.stdin.isatty() and not getattr(args, "non_interactive", False) and not (args.provider or args.model):
            run_interactive_setup(config)
        else:
            if args.provider:
                config.active_provider = args.provider
            if args.model:
                config.model_name = args.model
            if args.effort:
                config.reasoning_effort = args.effort
            config.save_config()

        if getattr(args, "is_global", False):
            path = store.init_global()
            print(f"✓ Initialized global memory at {path}")
        else:
            target_proj = Path.cwd().resolve() / ".agent-m3m"
            path = store.init_project(target_proj)
            print(f"✓ Initialized project memory at {path}")

        # Automatically wire agent hooks
        print("\n--- Wiring Agent Lifecycle Hooks ---")
        installer = HooksInstaller(home_dir=config.home_dir)
        hook_results = installer.install_all()
        for name, msg, ok in hook_results:
            symbol = "✓" if ok else "✗"
            print(f"[{symbol}] {name}: {msg}")

        print("\n✓ Setup completed successfully! Whenever you run Codex CLI or Claude Code, your memory will be loaded on start and updated on stop.")

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
        print("=== agent-m3m doctor ===\n")
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
