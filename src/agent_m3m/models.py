"""Data models and frontmatter parsing for agent-m3m."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


def generate_memory_id() -> str:
    """Generate a compact, unique memory ID (e.g. mem_01k8x9a2b)."""
    now = datetime.now(timezone.utc)
    date_prefix = now.strftime("%Y%m%d")
    rand_suffix = secrets.token_hex(4)
    return f"mem_{date_prefix}_{rand_suffix}"


@dataclass
class MemoryItem:
    id: str
    content: str
    scope: MemoryScope = MemoryScope.GLOBAL
    project_id: Optional[str] = None
    type: MemoryType = MemoryType.PREFERENCE
    status: MemoryStatus = MemoryStatus.ACTIVE
    confidence: float = 1.0
    tags: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    superseded_by: Optional[str] = None
    file_path: Optional[str] = None

    def to_markdown(self) -> str:
        """Serialize memory item to Markdown with YAML frontmatter."""
        tags_str = ", ".join(f'"{t}"' for t in self.tags)
        entities_str = ", ".join(f'"{e}"' for e in self.entities)
        proj_str = f'"{self.project_id}"' if self.project_id else "null"
        sup_str = f'"{self.superseded_by}"' if self.superseded_by else "null"

        frontmatter = (
            "---\n"
            f"id: \"{self.id}\"\n"
            f"created: \"{self.created}\"\n"
            f"updated: \"{self.updated}\"\n"
            f"scope: \"{self.scope.value if isinstance(self.scope, MemoryScope) else self.scope}\"\n"
            f"project_id: {proj_str}\n"
            f"type: \"{self.type.value if isinstance(self.type, MemoryType) else self.type}\"\n"
            f"status: \"{self.status.value if isinstance(self.status, MemoryStatus) else self.status}\"\n"
            f"confidence: {self.confidence}\n"
            f"tags: [{tags_str}]\n"
            f"entities: [{entities_str}]\n"
            f"superseded_by: {sup_str}\n"
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

            # Parse simple values
            if val == "null" or val == "None" or not val:
                metadata[key] = None
            elif val.startswith("[") and val.endswith("]"):
                # Parse simple list [a, b, "c"]
                items = val[1:-1].split(",")
                metadata[key] = [i.strip().strip('"').strip("'") for i in items if i.strip()]
            elif val.startswith('"') and val.endswith('"'):
                metadata[key] = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                metadata[key] = val[1:-1]
            else:
                try:
                    metadata[key] = float(val) if "." in val else int(val)
                except ValueError:
                    metadata[key] = val

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

        return cls(
            id=mem_id,
            content=content,
            scope=scope,
            project_id=metadata.get("project_id"),
            type=mtype,
            status=status,
            confidence=float(metadata.get("confidence", 1.0)),
            tags=metadata.get("tags") or [],
            entities=metadata.get("entities") or [],
            created=metadata.get("created") or datetime.now(timezone.utc).isoformat(),
            updated=metadata.get("updated") or datetime.now(timezone.utc).isoformat(),
            superseded_by=metadata.get("superseded_by"),
            file_path=file_path,
        )
