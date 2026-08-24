"""ContextBuilder: Assembles Core Memory and top relevant active memories for agent session start."""

from typing import Optional
from .store import MemoryStore
from .retriever import MemoryRetriever
from .models import MemoryScope
from .config import Config


class ContextBuilder:
    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        retriever: Optional[MemoryRetriever] = None,
        config: Optional[Config] = None,
    ):
        self.config = config or Config()
        self.store = store or MemoryStore(self.config)
        self.retriever = retriever or MemoryRetriever(self.store, self.config)

    def build_context(self, project_name: Optional[str] = None) -> str:
        """Build initial session context string."""
        global_core = self.store.read_core_memory(MemoryScope.GLOBAL)
        project_core = self.store.read_core_memory(MemoryScope.PROJECT)

        lines = [
            "<!-- AGENT PERSISTENT MEMORY CONTEXT -->",
            "Below is user memory context retrieved from ~/.agent-m3m.",
            "",
        ]

        if global_core.strip():
            lines.append("### Global Core Memory")
            lines.append(global_core.strip())
            lines.append("")

        if project_core.strip():
            lines.append("### Project Core Memory")
            lines.append(project_core.strip())
            lines.append("")

        # Fetch recent active project memories
        project_items = self.store.list_items(scope=MemoryScope.PROJECT, status="active")
        if project_items:
            lines.append("### Project Specific Learnings & Decisions")
            for item in project_items[:5]:
                lines.append(f"- [{item.type.value}] {item.content}")
            lines.append("")

        lines.append("<!-- END AGENT PERSISTENT MEMORY CONTEXT -->")
        return "\n".join(lines)
