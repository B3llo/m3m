"""MemoryStore: File-system storage layer for atomic Markdown memories."""

from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime, timezone

from .models import MemoryItem, MemoryScope, MemoryType, MemoryStatus, generate_memory_id
from .sanitizer import scrub_secrets
from .config import Config


class MemoryStore:
    SUBDIRS = [
        "preferences",
        "lessons",
        "decisions",
        "conventions",
        "tools",
        "project_facts",
        "workflows",
    ]

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.global_dir = self.config.global_dir
        self.project_dir = self.config.project_dir

    def init_global(self) -> Path:
        """Initialize global memory directory structure."""
        self.global_dir.mkdir(parents=True, exist_ok=True)
        for sub in self.SUBDIRS:
            (self.global_dir / sub).mkdir(exist_ok=True)
        (self.global_dir / "sessions").mkdir(exist_ok=True)
        (self.global_dir / ".index").mkdir(exist_ok=True)

        core_file = self.global_dir / "CORE.md"
        if not core_file.exists():
            core_file.write_text(
                "# Global Core Memory\n\n"
                "## Preferences\n"
                "- Prefers local solutions over SaaS services.\n"
                "- Prefers pnpm over npm for Node.js projects.\n\n"
                "## Universal Conventions\n"
                "- Write modular, well-tested, clean code.\n",
                encoding="utf-8",
            )
        self.config.save_default_config()
        return self.global_dir

    def init_project(self, project_path: Optional[Path] = None) -> Path:
        """Initialize project memory directory structure."""
        target_dir = project_path or (Path.cwd().resolve() / ".agent-m3m")
        self.project_dir = target_dir

        target_dir.mkdir(parents=True, exist_ok=True)
        for sub in self.SUBDIRS:
            (target_dir / sub).mkdir(exist_ok=True)
        (target_dir / "sessions").mkdir(exist_ok=True)
        (target_dir / ".index").mkdir(exist_ok=True)

        core_file = target_dir / "CORE.md"
        if not core_file.exists():
            proj_name = target_dir.parent.name
            core_file.write_text(
                f"# Project Core Memory: {proj_name}\n\n"
                "## Project Decisions\n"
                "- Add key project decisions here.\n",
                encoding="utf-8",
            )

        # Add to .gitignore if inside a git repo
        git_ignore = target_dir.parent / ".gitignore"
        if git_ignore.exists():
            content = git_ignore.read_text(encoding="utf-8")
            if ".agent-m3m/sessions" not in content:
                git_ignore.write_text(
                    content + "\n# agent-m3m temp caches\n.agent-m3m/sessions/\n.agent-m3m/.index/\n",
                    encoding="utf-8",
                )

        return target_dir

    def _get_target_dir(self, item: MemoryItem) -> Path:
        """Determine target directory for a memory item based on scope and type."""
        base_dir = self.global_dir if item.scope == MemoryScope.GLOBAL else self.project_dir
        if not base_dir.exists():
            if item.scope == MemoryScope.GLOBAL:
                self.init_global()
            else:
                self.init_project()

        type_subdir_map = {
            MemoryType.PREFERENCE: "preferences",
            MemoryType.LESSON: "lessons",
            MemoryType.DECISION: "decisions",
            MemoryType.CONVENTION: "conventions",
            MemoryType.TOOL: "tools",
            MemoryType.PROJECT_FACT: "project_facts",
            MemoryType.WORKFLOW: "workflows",
        }
        subdir = type_subdir_map.get(item.type, "preferences")
        folder = base_dir / subdir
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def save_item(self, item: MemoryItem) -> MemoryItem:
        """Save memory item to Markdown file, scrubbing secrets first."""
        if self.config.secret_scrubbing:
            item.content = scrub_secrets(item.content)

        item.updated = datetime.now(timezone.utc).isoformat()
        folder = self._get_target_dir(item)
        file_path = folder / f"{item.id}.md"
        file_path.write_text(item.to_markdown(), encoding="utf-8")
        item.file_path = str(file_path)
        return item

    def load_item_by_id(self, item_id: str) -> Optional[MemoryItem]:
        """Find and load a memory item by ID across global and project storage."""
        items = self.list_items()
        for item in items:
            if item.id == item_id:
                return item
        return None

    def list_items(
        self,
        scope: Optional[MemoryScope] = None,
        status: Optional[MemoryStatus] = None,
    ) -> List[MemoryItem]:
        """Scan global and project directories for memory Markdown files."""
        items: List[MemoryItem] = []
        seen_paths = set()
        target_roots = []

        if scope is None or scope == MemoryScope.GLOBAL:
            if self.global_dir.exists():
                target_roots.append(self.global_dir)

        if scope is None or scope == MemoryScope.PROJECT:
            if self.project_dir and self.project_dir.exists() and self.project_dir.resolve() != self.global_dir.resolve():
                target_roots.append(self.project_dir)

        for root in target_roots:
            for sub in self.SUBDIRS:
                folder = root / sub
                if not folder.exists():
                    continue
                for file in folder.glob("*.md"):
                    resolved = file.resolve()
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)
                    try:
                        content = file.read_text(encoding="utf-8")
                        item = MemoryItem.from_markdown(content, file_path=str(file))
                        if status is None or item.status == status:
                            items.append(item)
                    except Exception:
                        continue
        return items

    def update_status(
        self,
        item_id: str,
        new_status: MemoryStatus,
        superseded_by: Optional[str] = None,
    ) -> bool:
        """Update status and optional superseded_by link for an existing memory item."""
        item = self.load_item_by_id(item_id)
        if not item:
            return False

        item.status = new_status
        if superseded_by:
            item.superseded_by = superseded_by
        self.save_item(item)
        return True

    def read_core_memory(self, scope: MemoryScope = MemoryScope.GLOBAL) -> str:
        """Read core memory content for specified scope."""
        target_file = (
            self.global_dir / "CORE.md"
            if scope == MemoryScope.GLOBAL
            else (self.project_dir / "CORE.md" if self.project_dir else None)
        )

        if target_file and target_file.is_file():
            return target_file.read_text(encoding="utf-8")
        return ""
