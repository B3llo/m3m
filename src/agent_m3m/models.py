"""Data models and frontmatter parsing for agent-m3m."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import ast
import csv
import io
import json
import re
import secrets
from typing import List, Optional, Dict, Any


class MemoryScope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"


class MemoryType(str, Enum):
    PREFERENCE = "preference"
    LESSON = "lesson"
    DECISION = "decision"
    CONVENTION = "convention"
    TOOL = "tool"
    PROJECT_FACT = "project_fact"
    WORKFLOW = "workflow"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    PENDING = "pending"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


# Shared display order for generated overviews (CORE.md, INDEX.md,
# universal exports). Mirrors MemoryType values.
SECTION_TITLES = {
    MemoryType.DECISION: "Decisions",
    MemoryType.CONVENTION: "Conventions",
    MemoryType.PREFERENCE: "Preferences",
    MemoryType.WORKFLOW: "Workflows & Commands",
    MemoryType.LESSON: "Lessons",
    MemoryType.TOOL: "Tools",
    MemoryType.PROJECT_FACT: "Project Facts",
}


def generate_memory_id() -> str:
    """Generate a compact, unique memory ID (e.g. mem_01k8x9a2b)."""
    now = datetime.now(timezone.utc)
    date_prefix = now.strftime("%Y%m%d")
    rand_suffix = secrets.token_hex(4)
    return f"mem_{date_prefix}_{rand_suffix}"


def normalize_topic(value: Any) -> str:
    """Normalize a free-form topic label into a stable lowercase-hyphen slug."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug[:60]


@dataclass
class MemoryItem:
    id: str
    content: str
    scope: MemoryScope = MemoryScope.GLOBAL
    project_id: Optional[str] = None
    type: MemoryType = MemoryType.PREFERENCE
    status: MemoryStatus = MemoryStatus.ACTIVE
    confidence: float = 1.0
    topic: str = ""
    tags: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    superseded_by: Optional[str] = None
    source: str = "manual"
    source_session: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
    expires_at: Optional[str] = None
    last_used: Optional[str] = None
    use_count: int = 0
    # Corroboration: how many times later extractions re-observed this memory
    # instead of adding a duplicate. Additive signal only; never drives
    # deletion. Missing on legacy files -> 0/None.
    corroborated_count: int = 0
    last_corroborated: Optional[str] = None
    pinned: bool = False
    file_path: Optional[str] = None

    @staticmethod
    def _quote(value: Any) -> str:
        """Quote a scalar using JSON, which is valid YAML and round-trips safely."""
        if value is None:
            return "null"
        return json.dumps(str(value), ensure_ascii=False)

    @staticmethod
    def _parse_value(value: str) -> Any:
        """Parse the small YAML subset used by memory files.

        New files use JSON-compatible values. The fallbacks keep older hand-written
        frontmatter readable, including lists containing commas or escaped quotes.
        """
        value = value.strip()
        if value.lower() in {"null", "none", "~"} or not value:
            return None

        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False

        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                pass
            try:
                parsed = ast.literal_eval(value)
                return parsed if isinstance(parsed, list) else []
            except (SyntaxError, ValueError):
                pass
            try:
                return next(csv.reader([value[1:-1]], skipinitialspace=True))
            except (StopIteration, csv.Error):
                return []

        if value.startswith(('"', "'")) and value.endswith(value[0]):
            try:
                return json.loads(value) if value[0] == '"' else ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return value[1:-1]

        try:
            return float(value) if any(c in value for c in ".eE") else int(value)
        except ValueError:
            return value

    def to_markdown(self) -> str:
        """Serialize memory item to Markdown with YAML frontmatter."""
        frontmatter = (
            "---\n"
            f"id: {self._quote(self.id)}\n"
            f"created: {self._quote(self.created)}\n"
            f"updated: {self._quote(self.updated)}\n"
            f"scope: {self._quote(self.scope.value if isinstance(self.scope, MemoryScope) else self.scope)}\n"
            f"project_id: {self._quote(self.project_id)}\n"
            f"type: {self._quote(self.type.value if isinstance(self.type, MemoryType) else self.type)}\n"
            f"status: {self._quote(self.status.value if isinstance(self.status, MemoryStatus) else self.status)}\n"
            f"confidence: {float(self.confidence):.6g}\n"
            f"topic: {self._quote(self.topic or None)}\n"
            f"tags: {json.dumps(self.tags, ensure_ascii=False)}\n"
            f"entities: {json.dumps(self.entities, ensure_ascii=False)}\n"
            f"superseded_by: {self._quote(self.superseded_by)}\n"
            f"source: {self._quote(self.source)}\n"
            f"source_session: {self._quote(self.source_session)}\n"
            f"evidence: {json.dumps(self.evidence, ensure_ascii=False)}\n"
            f"related: {json.dumps(self.related, ensure_ascii=False)}\n"
            f"expires_at: {self._quote(self.expires_at)}\n"
            f"last_used: {self._quote(self.last_used)}\n"
            f"use_count: {int(self.use_count)}\n"
            f"corroborated_count: {int(self.corroborated_count)}\n"
            f"last_corroborated: {self._quote(self.last_corroborated)}\n"
            f"pinned: {'true' if self.pinned else 'false'}\n"
            "---\n\n"
            f"{self.content.strip()}\n"
        )
        return frontmatter

    @classmethod
    def from_markdown(cls, raw_markdown: str, file_path: Optional[str] = None) -> "MemoryItem":
        """Parse raw Markdown string into MemoryItem."""
        frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw_markdown, re.DOTALL)
        if not frontmatter_match:
            # Fallback if no frontmatter exists
            file_id = generate_memory_id()
            return cls(id=file_id, content=raw_markdown.strip(), file_path=file_path)

        yaml_text = frontmatter_match.group(1)
        content = frontmatter_match.group(2).strip()

        metadata: Dict[str, Any] = {}
        for line in yaml_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()

            metadata[key] = cls._parse_value(val)

        mem_id = metadata.get("id") or generate_memory_id()
        scope_val = metadata.get("scope") or "global"
        type_val = metadata.get("type") or "preference"
        status_val = metadata.get("status") or "active"

        try:
            scope = MemoryScope(scope_val)
        except ValueError:
            scope = MemoryScope.GLOBAL

        try:
            mtype = MemoryType(type_val)
        except ValueError:
            mtype = MemoryType.PREFERENCE

        try:
            status = MemoryStatus(status_val)
        except ValueError:
            status = MemoryStatus.ACTIVE

        tags = metadata.get("tags") or []
        entities = metadata.get("entities") or []
        evidence = metadata.get("evidence") or []
        related = metadata.get("related") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        if not isinstance(entities, list):
            entities = [str(entities)]
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        if not isinstance(related, list):
            related = [str(related)]

        return cls(
            id=mem_id,
            content=content,
            scope=scope,
            project_id=metadata.get("project_id"),
            type=mtype,
            status=status,
            confidence=float(metadata.get("confidence", 1.0)),
            topic=normalize_topic(metadata.get("topic")),
            tags=[str(value) for value in tags],
            entities=[str(value) for value in entities],
            created=metadata.get("created") or datetime.now(timezone.utc).isoformat(),
            updated=metadata.get("updated") or datetime.now(timezone.utc).isoformat(),
            superseded_by=metadata.get("superseded_by"),
            source=metadata.get("source") or "legacy",
            source_session=metadata.get("source_session"),
            evidence=[str(value) for value in evidence],
            related=[str(value) for value in related],
            expires_at=metadata.get("expires_at"),
            last_used=metadata.get("last_used"),
            use_count=int(metadata.get("use_count") or 0),
            corroborated_count=int(metadata.get("corroborated_count") or 0),
            last_corroborated=metadata.get("last_corroborated"),
            pinned=bool(metadata.get("pinned") or False),
            file_path=file_path,
        )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Return whether the optional expiration timestamp has passed."""
        if not self.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry <= (now or datetime.now(timezone.utc))
        except ValueError:
            return False
