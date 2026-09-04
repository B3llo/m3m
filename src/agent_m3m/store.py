"""MemoryStore: File-system storage layer for atomic Markdown memories."""

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from contextlib import contextmanager
import json
import os
import re
import tempfile
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from .models import MemoryItem, MemoryScope, MemoryType, MemoryStatus, generate_memory_id, normalize_topic
from .sanitizer import scrub_secrets
from .config import Config


PROJECT_MEMORY_GITIGNORE_ENTRY = "/.agent-m3m/"
AGENTS_EXPORT_GITIGNORE_ENTRY = "/AGENTS.md"
MEMORY_EXPORT_GITIGNORE_ENTRY = "/MEMORY.md"
LOCAL_ARTIFACT_GITIGNORE_ENTRIES = frozenset({
    PROJECT_MEMORY_GITIGNORE_ENTRY,
    AGENTS_EXPORT_GITIGNORE_ENTRY,
    MEMORY_EXPORT_GITIGNORE_ENTRY,
})


def ensure_local_artifacts_ignored(project_root: Path, entries: Tuple[str, ...]) -> Path:
    """Append selected local-artifact protections without rewriting existing bytes."""
    gitignore = project_root / ".gitignore"
    requested = tuple(dict.fromkeys(entries))
    unsupported = set(requested) - LOCAL_ARTIFACT_GITIGNORE_ENTRIES
    if unsupported:
        raise ValueError(f"Unsupported agent-m3m gitignore entries: {sorted(unsupported)}")
    if not requested:
        return gitignore

    existing = gitignore.read_bytes() if gitignore.exists() else b""
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [
        entry.encode("utf-8")
        for entry in requested
        if entry.encode("utf-8") not in existing_lines
    ]
    if not missing:
        return gitignore

    newline = b"\r\n" if b"\r\n" in existing else b"\n"
    prefix = b"" if not existing or existing.endswith(b"\n") else newline
    addition = prefix + newline.join(missing) + newline
    gitignore.parent.mkdir(parents=True, exist_ok=True)
    with gitignore.open("ab") as handle:
        handle.write(addition)
    return gitignore


class MemoryStore:
    APPROVAL_RECONCILIATION_DIR = "approval-reconciliation"
    MEMORY_QUARANTINE_DIR = "memory-quarantine"
    TOPICS_DIR = "topics"
    SUBDIRS = [
        "preferences",
        "lessons",
        "decisions",
        "conventions",
        "tools",
        "project_facts",
        "workflows",
    ]
    # Record injection feedback at most once per interval per item so hook
    # bursts do not rewrite every memory file on each prompt.
    USAGE_WRITE_INTERVAL_SECONDS = 20 * 60 * 60

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.global_dir = self.config.global_dir
        self.project_dir = self.config.project_dir
        self.last_errors: List[str] = []

    def init_global(self) -> Path:
        """Initialize global memory directory structure with clean empty headers."""
        self.global_dir.mkdir(parents=True, exist_ok=True)
        for sub in self.SUBDIRS:
            (self.global_dir / sub).mkdir(exist_ok=True)
        (self.global_dir / "sessions").mkdir(exist_ok=True)
        (self.global_dir / ".index").mkdir(exist_ok=True)

        core_file = self.global_dir / "CORE.md"
        if not core_file.exists():
            core_file.write_text(
                "# Global Core Memory\n\n"
                "## Preferences\n\n"
                "## Universal Conventions\n",
                encoding="utf-8",
            )
        self.config.save_default_config()
        return self.global_dir

    def init_project(self, project_path: Optional[Path] = None) -> Path:
        """Initialize project memory directory structure with clean empty headers."""
        target_dir = project_path or self.project_dir or (Path.cwd().resolve() / ".agent-m3m")
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
                "## Project Decisions\n",
                encoding="utf-8",
            )

        if target_dir.name == ".agent-m3m":
            ensure_local_artifacts_ignored(target_dir.parent, (PROJECT_MEMORY_GITIGNORE_ENTRY,))

        return target_dir

    def _get_target_dir(self, item: MemoryItem) -> Path:
        """Determine target directory for a memory item based on scope and type."""
        base_dir = self.global_dir if item.scope == MemoryScope.GLOBAL else self.project_dir
        if base_dir is None or not base_dir.exists():
            if item.scope == MemoryScope.GLOBAL:
                self.init_global()
            else:
                self.init_project()
            base_dir = self.global_dir if item.scope == MemoryScope.GLOBAL else self.project_dir
        if base_dir is None:
            raise RuntimeError("Project memory directory is not configured")

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

    @contextmanager
    def _write_lock(self, base_dir: Path):
        """Serialize writers without adding a runtime dependency."""
        base_dir.mkdir(parents=True, exist_ok=True)
        lock_path = base_dir / ".store.lock"
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def _atomic_write(self, file_path: Path, content: str, private: bool = False) -> None:
        """Write a memory file atomically and make secret-bearing global files private."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(file_path.parent),
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600 if private else 0o644)
            os.replace(temp_path, file_path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    def _effect_base_dir(self) -> Optional[Path]:
        """Return the local side-effect checkpoint directory."""
        base_dir = self.project_dir or self.global_dir
        if not base_dir:
            return None
        return base_dir / "sessions" / "memory-effects"

    @staticmethod
    def _effect_filename(idempotency_key: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(idempotency_key))[:120]
        return f"{safe or 'unknown'}.json"

    def load_effect(self, idempotency_key: Optional[str]) -> Optional[dict]:
        """Load a memory effect checkpoint without treating it as source memory."""
        if not idempotency_key:
            return None
        base_dir = self._effect_base_dir()
        if not base_dir:
            return None
        path = base_dir / self._effect_filename(idempotency_key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save_effect(self, idempotency_key: Optional[str], effect: dict) -> None:
        """Persist a bounded, scrubbed memory effect checkpoint atomically."""
        if not idempotency_key:
            return
        if not isinstance(effect, dict):
            raise ValueError("memory effect must be an object")
        base_dir = self._effect_base_dir()
        if not base_dir:
            raise RuntimeError("memory effect directory is not configured")
        safe_effect = {}
        for key, value in effect.items():
            if isinstance(value, list):
                safe_effect[str(key)[:100]] = [scrub_secrets(str(item))[:300] for item in value[:100]]
            elif isinstance(value, (bool, int, float)) or value is None:
                safe_effect[str(key)[:100]] = value
            else:
                safe_effect[str(key)[:100]] = scrub_secrets(str(value))[:2000]
        path = base_dir / self._effect_filename(idempotency_key)
        encoded = json.dumps(safe_effect, indent=2, ensure_ascii=False) + "\n"
        with self._write_lock(base_dir.parent):
            self._atomic_write(path, encoded, private=True)

    def _normalize_item_for_persist(self, item: MemoryItem) -> None:
        """Normalize all persisted fields before either normal or recovery writes."""
        if self.config.secret_scrubbing:
            item.content = scrub_secrets(item.content)
            item.tags = [scrub_secrets(str(tag)) for tag in item.tags]
            item.entities = [scrub_secrets(str(entity)) for entity in item.entities]
            item.evidence = [scrub_secrets(str(evidence)) for evidence in item.evidence]

        # These fields are persisted in frontmatter and must be scrubbed even
        # when general content scrubbing is disabled. Session IDs frequently
        # contain provider metadata or accidental credentials.
        item.source = scrub_secrets(str(item.source or ""))[:200] or "manual"
        if item.source_session is not None:
            item.source_session = scrub_secrets(str(item.source_session))[:500]

        item.updated = datetime.now(timezone.utc).isoformat()

    def _persist_item_direct(self, item: MemoryItem) -> MemoryItem:
        """Persist an item without the public save_item override path."""
        self._normalize_item_for_persist(item)
        folder = self._get_target_dir(item)
        file_path = folder / f"{item.id}.md"
        base_dir = self.global_dir if item.scope == MemoryScope.GLOBAL else self.project_dir
        if base_dir is None:
            raise RuntimeError("Project memory directory is not configured")
        with self._write_lock(base_dir):
            self._atomic_write(file_path, item.to_markdown(), private=item.scope == MemoryScope.GLOBAL)
        item.file_path = str(file_path)
        return item

    def save_item(self, item: MemoryItem) -> MemoryItem:
        """Save memory item to Markdown file, scrubbing secrets first."""
        return self._persist_item_direct(item)

    def _approval_reconciliation_dir(self, item: MemoryItem) -> Path:
        base_dir = self.global_dir if item.scope == MemoryScope.GLOBAL else self.project_dir
        if base_dir is None:
            raise RuntimeError("Project memory directory is not configured")
        return base_dir / "sessions" / self.APPROVAL_RECONCILIATION_DIR

    @staticmethod
    def _safe_reconciliation_filename(item_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", str(item_id))[:120] or "unknown"

    def record_approval_reconciliation(
        self,
        new_item: MemoryItem,
        old_item: Optional[MemoryItem],
        reason: str,
    ) -> Path:
        """Durably record the safe state for a failed approval transition."""
        directory = self._approval_reconciliation_dir(new_item)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._safe_reconciliation_filename(new_item.id)}.json"
        data = {
            "new_item_id": scrub_secrets(str(new_item.id))[:200],
            "old_item_id": scrub_secrets(str(old_item.id))[:200] if old_item else "",
            "desired_new_status": MemoryStatus.PENDING.value,
            "desired_old_status": MemoryStatus.ACTIVE.value,
            "reason": scrub_secrets(str(reason))[:2000],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._write_lock(directory.parent):
            self._atomic_write(
                path,
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                private=True,
            )
        return path

    def has_approval_reconciliation(self, item_id: str) -> bool:
        """Return whether an unfinished approval intent exists for an item."""
        safe_name = self._safe_reconciliation_filename(item_id)
        roots = [self.global_dir]
        if self.project_dir and self.project_dir.resolve() != self.global_dir.resolve():
            roots.append(self.project_dir)
        return any(
            (root / "sessions" / self.APPROVAL_RECONCILIATION_DIR / f"{safe_name}.json").is_file()
            for root in roots
        )

    def _load_approval_reconciliations(self) -> dict:
        """Load durable approval overrides without making them source memory."""
        reconciliations = {}
        roots = [self.global_dir]
        if self.project_dir and self.project_dir.resolve() != self.global_dir.resolve():
            roots.append(self.project_dir)
        for root in roots:
            directory = root / "sessions" / self.APPROVAL_RECONCILIATION_DIR
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    new_id = str(data.get("new_item_id", ""))
                    old_id = str(data.get("old_item_id", ""))
                    if new_id:
                        reconciliations[new_id] = old_id
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        return reconciliations

    def clear_approval_reconciliation(self, item_id: str) -> None:
        """Remove a resolved approval journal from all configured scopes."""
        safe_name = self._safe_reconciliation_filename(item_id)
        roots = [self.global_dir]
        if self.project_dir and self.project_dir.resolve() != self.global_dir.resolve():
            roots.append(self.project_dir)
        for root in roots:
            path = root / "sessions" / self.APPROVAL_RECONCILIATION_DIR / f"{safe_name}.json"
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                self.last_errors.append(f"could not clear approval reconciliation: {path}")

    def quarantine_item(self, item: MemoryItem, reason: str) -> Path:
        """Move an unreconcilable item outside the memory scan tree."""
        base_dir = self.global_dir if item.scope == MemoryScope.GLOBAL else self.project_dir
        if base_dir is None:
            raise RuntimeError("Project memory directory is not configured")
        directory = base_dir / "sessions" / self.MEMORY_QUARANTINE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{self._safe_reconciliation_filename(item.id)}_{uuid.uuid4().hex[:12]}.md"
        source = Path(item.file_path) if item.file_path else None
        if source is None or not source.is_file():
            source = self._get_target_dir(item) / f"{item.id}.md"
        with self._write_lock(base_dir):
            if source.is_file():
                os.replace(source, target)
            else:
                self._normalize_item_for_persist(item)
                self._atomic_write(target, item.to_markdown(), private=item.scope == MemoryScope.GLOBAL)
        self.last_errors.append(
            f"quarantined memory {item.id}: {scrub_secrets(str(reason))[:500]}"
        )
        return target

    def reconcile_approval(
        self,
        new_item: MemoryItem,
        old_item: MemoryItem,
        reason: str,
    ) -> Path:
        """Restore the safe states after an approval transition fails twice."""
        new_item.status = MemoryStatus.PENDING
        new_item.superseded_by = old_item.id
        try:
            journal_path = self.record_approval_reconciliation(new_item, old_item, reason)
        except Exception as journal_exc:
            # If the journal itself cannot be written, remove the candidate
            # from the active memory tree before surfacing the failure. The old
            # snapshot remains the only visible source of truth.
            try:
                quarantine_path = self.quarantine_item(
                    new_item,
                    f"approval journal write failed: {journal_exc}",
                )
            except Exception as quarantine_exc:
                raise RuntimeError(
                    f"approval journal failed: {journal_exc}; "
                    f"item quarantine failed: {quarantine_exc}"
                ) from journal_exc
            return quarantine_path

        try:
            # Restore the old snapshot first in case its failed write partially
            # reached disk, then make the new candidate pending. Both writes
            # bypass save_item so compensation remains available when that
            # public method itself is the failing operation.
            old_item.status = MemoryStatus.ACTIVE
            self._persist_item_direct(old_item)
            self._persist_item_direct(new_item)
        except Exception as exc:
            try:
                self.quarantine_item(new_item, f"approval reconciliation write failed: {exc}")
            except Exception as quarantine_exc:
                raise RuntimeError(
                    f"approval journal {journal_path} written, but item quarantine failed: "
                    f"{quarantine_exc}"
                ) from exc
        return journal_path

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
        include_expired: bool = False,
    ) -> List[MemoryItem]:
        """Scan global and project directories for memory Markdown files."""
        items: List[MemoryItem] = []
        self.last_errors = []
        approval_reconciliations = self._load_approval_reconciliations()
        approval_old_ids = {old_id for old_id in approval_reconciliations.values() if old_id}
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
                        if item.id in approval_reconciliations:
                            item.status = MemoryStatus.PENDING
                            item.superseded_by = approval_reconciliations[item.id]
                        elif item.id in approval_old_ids:
                            # An approval intent is the authoritative recovery
                            # barrier.  Never expose its two versions as active
                            # together, even after an abrupt process stop
                            # between their individual Markdown writes.
                            item.status = MemoryStatus.ACTIVE
                        status_value = status.value if isinstance(status, MemoryStatus) else status
                        item_status = item.status.value if isinstance(item.status, MemoryStatus) else item.status
                        if item.is_expired() and not include_expired:
                            continue
                        if status_value is None or item_status == status_value:
                            items.append(item)
                    except Exception as exc:
                        self.last_errors.append(f"{file}: {exc}")
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

    def archive_by_status(self, status: MemoryStatus) -> int:
        """Archive every memory with the given status, keeping its history."""
        items = self.list_items(status=status)
        for item in items:
            item.status = MemoryStatus.ARCHIVED
            self.save_item(item)
        return len(items)

    def archive_all_active(self) -> int:
        """Archive every active memory without deleting its Markdown history."""
        return self.archive_by_status(MemoryStatus.ACTIVE)

    def stats(self) -> dict:
        """Return deterministic storage counts for CLI and diagnostics."""
        all_items = self.list_items(include_expired=True)
        stats = {"total": len(all_items), "global": 0, "project": 0, "by_status": {}}
        for item in all_items:
            scope = item.scope.value if isinstance(item.scope, MemoryScope) else str(item.scope)
            status = item.status.value if isinstance(item.status, MemoryStatus) else str(item.status)
            if scope in {"global", "project"}:
                stats[scope] += 1
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1
        stats["parse_errors"] = len(self.last_errors)
        return stats

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

    def write_overview(self, scope: MemoryScope, filename: str, content: str) -> Optional[Path]:
        """Atomically write a derived overview file (CORE.md, INDEX.md)."""
        base = self.global_dir if scope == MemoryScope.GLOBAL else self.project_dir
        if base is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
        target = base / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, content)
        return target

    def write_core_memory(self, scope: MemoryScope, content: str) -> Optional[Path]:
        """Atomically rewrite the CORE.md overview for a scope."""
        return self.write_overview(scope, "CORE.md", content)

    def _topics_base(self) -> Path:
        """Topic pages live in the project store (fallback: global)."""
        base = self.project_dir or self.global_dir
        if base is None:
            raise RuntimeError("Memory directory is not configured")
        return base / self.TOPICS_DIR

    def topic_page_path(self, slug: str) -> Path:
        safe = re.sub(r"[^a-z0-9-]", "-", normalize_topic(slug)) or "untitled"
        return self._topics_base() / f"{safe}.md"

    def write_topic_page(self, slug: str, content: str) -> Path:
        """Atomically write a derived, compiled topic page (LLM digest)."""
        path = self.topic_page_path(slug)
        self._atomic_write(path, content)
        return path

    def read_topic_page(self, slug: str) -> str:
        """Read a compiled topic page, or empty string when absent."""
        path = self.topic_page_path(slug)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def list_topic_pages(self) -> Dict[str, Path]:
        """Return {slug: path} for every compiled topic page."""
        base = self._topics_base()
        pages: Dict[str, Path] = {}
        if not base.is_dir():
            return pages
        for path in sorted(base.glob("*.md")):
            slug = path.stem
            if slug == "INDEX":
                continue
            pages[slug] = path
        return pages

    @staticmethod
    def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError):
            return None

    def record_usage(self, item_ids: List[str], now: Optional[datetime] = None) -> int:
        """Reinforce injected memories by bumping use_count/last_used in Markdown.

        This is the value signal behind consolidation: memories that keep being
        injected are the ones worth promoting to CORE.md.  Writes are bounded
        (top 20 ids, once per interval per item) and never touch `updated`, so
        content recency keeps its meaning in ranking.
        """
        wanted = {str(value) for value in item_ids[:20] if value}
        if not wanted:
            return 0
        current = now or datetime.now(timezone.utc)
        bumped = 0
        for item in self.list_items():
            if item.id not in wanted:
                continue
            last_used = self._parse_timestamp(item.last_used)
            if last_used and (current - last_used).total_seconds() < self.USAGE_WRITE_INTERVAL_SECONDS:
                continue
            item.use_count = int(item.use_count or 0) + 1
            item.last_used = current.isoformat()
            try:
                base_dir = self.global_dir if item.scope == MemoryScope.GLOBAL else self.project_dir
                if base_dir is None:
                    continue
                folder = self._get_target_dir(item)
                with self._write_lock(base_dir):
                    self._atomic_write(
                        folder / f"{item.id}.md",
                        item.to_markdown(),
                        private=item.scope == MemoryScope.GLOBAL,
                    )
                bumped += 1
            except Exception as exc:
                self.last_errors.append(f"could not record usage for {item.id}: {exc}")
        return bumped
