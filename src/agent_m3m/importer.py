"""Importer / Migrator module for m3m.

Imports and migrates legacy memories from popular agent memory tools:
- jayzeng/agentmemory (~/.agent-memory/)
- claude-mem / Claude memory (~/.claude/memory/ or ~/.claude-mem/)
- Project steering files (CLAUDE.md, AGENTS.md, .cursorrules)
"""

from pathlib import Path
from typing import List, Tuple, Optional
import re

from .models import MemoryItem, MemoryScope, MemoryType, MemoryStatus, generate_memory_id
from .sanitizer import scrub_secrets
from .store import MemoryStore
from .retriever import MemoryRetriever
from .config import Config


class MemoryImporter:
    def __init__(self, store: Optional[MemoryStore] = None, config: Optional[Config] = None):
        self.config = config or Config()
        self.store = store or MemoryStore(self.config)
        self.retriever = MemoryRetriever(self.store, self.config)

    def scan_sources(self) -> List[Tuple[str, Path, int]]:
        """Scan system for importable legacy memory sources."""
        sources: List[Tuple[str, Path, int]] = []
        home = self.config.home_dir

        # 1. jayzeng/agentmemory
        legacy_dir = home / ".agent-memory"
        if legacy_dir.is_dir() and legacy_dir != self.config.global_dir:
            md_files = list(legacy_dir.glob("**/*.md"))
            if md_files:
                sources.append(("jayzeng/agentmemory (~/.agent-memory)", legacy_dir, len(md_files)))

        # 2. claude-mem / Claude memory
        claude_mem_dir = home / ".claude" / "memory"
        if claude_mem_dir.is_dir():
            md_files = list(claude_mem_dir.glob("**/*.md"))
            if md_files:
                sources.append(("claude-mem (~/.claude/memory)", claude_mem_dir, len(md_files)))

        claude_mem_dir2 = home / ".claude-mem"
        if claude_mem_dir2.is_dir():
            md_files = list(claude_mem_dir2.glob("**/*.md"))
            if md_files:
                sources.append(("claude-mem (~/.claude-mem)", claude_mem_dir2, len(md_files)))

        # 3. Project specific steering files in current working directory
        cwd = Path.cwd()
        for steering_name in ["CLAUDE.md", "AGENTS.md", ".cursorrules"]:
            st_file = cwd / steering_name
            if st_file.is_file():
                sources.append((f"Project file ({steering_name})", st_file, 1))

        return sources

    def import_from_source(self, source_path: Path) -> List[MemoryItem]:
        """Import memories from a file or folder source into m3m."""
        imported: List[MemoryItem] = []

        if source_path.is_file():
            items = self._parse_file(source_path)
            imported.extend(items)
        elif source_path.is_dir():
            for file_path in source_path.glob("**/*.md"):
                if file_path.name.startswith(".") or "sessions" in file_path.parts:
                    continue
                items = self._parse_file(file_path)
                imported.extend(items)

        saved_items = []
        for item in imported:
            item.content = scrub_secrets(item.content)
            if not item.content.strip():
                continue
            saved = self.store.save_item(item)
            saved_items.append(saved)

        if saved_items:
            self.retriever.reindex()

        return saved_items

    def _parse_file(self, file_path: Path) -> List[MemoryItem]:
        """Parse raw memory markdown or steering file into MemoryItems."""
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return []

        items: List[MemoryItem] = []

        # If it has m3m or YAML frontmatter, use standard parser
        if content.startswith("---"):
            item = MemoryItem.from_markdown(content, file_path=str(file_path))
            item.id = generate_memory_id()
            items.append(item)
            return items

        # For plain Markdown or CLAUDE.md / AGENTS.md, parse list items or bullet points
        lines = content.splitlines()
        current_type = MemoryType.PREFERENCE

        for line in lines:
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue

            if line_str.startswith("- ") or line_str.startswith("* "):
                fact_str = line_str[2:].strip()
                if len(fact_str) < 8 or len(fact_str) > 400:
                    continue

                item = MemoryItem(
                    id=generate_memory_id(),
                    content=fact_str,
                    scope=MemoryScope.GLOBAL if "~" in str(file_path) else MemoryScope.PROJECT,
                    type=current_type,
                    confidence=0.9,
                    tags=["migrated", file_path.stem.lower()],
                )
                items.append(item)

        # Fallback if no bullet points found: treat whole file as a single memory if short enough
        if not items and len(content.strip()) > 10 and len(content.strip()) < 1000:
            item = MemoryItem(
                id=generate_memory_id(),
                content=content.strip(),
                scope=MemoryScope.GLOBAL if "~" in str(file_path) else MemoryScope.PROJECT,
                type=MemoryType.PROJECT_FACT,
                confidence=0.85,
                tags=["migrated"],
            )
            items.append(item)

        return items
