"""MemoryExtractor: Extracts durable memories from session transcripts using LLM structured JSON output."""

import json
import re
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from .models import MemoryItem, MemoryScope, MemoryType, MemoryStatus, generate_memory_id
from .sanitizer import scrub_secrets
from .store import MemoryStore
from .retriever import MemoryRetriever
from .llm_runner import LLMRunner
from .config import Config


EXTRACTION_SYSTEM_PROMPT = """Analyze the provided AI coding session transcript and extract DURABLE, REUSABLE memories.

Only extract:
1. User preferences (e.g. preferred tools, package managers, local vs SaaS choices).
2. Architectural decisions made for this project.
3. Lessons learned (e.g. fixing a specific build error, package incompatibility).
4. Coding conventions & rules explicitly requested.

Do NOT extract:
- Temporary chat banter or generic code explanations.
- Output logs, raw stack traces, or entire file contents.
- Obvious facts derived directly from code files.
- Secrets, passwords, API keys, or private environment variables.

Return ONLY a valid JSON object matching this schema:
{
  "memories": [
    {
      "content": "Clear summary of the memory",
      "scope": "global" or "project",
      "type": "preference" | "lesson" | "decision" | "convention" | "tool" | "project_fact" | "workflow",
      "confidence": 0.95,
      "tags": ["tag1", "tag2"],
      "entities": ["entity1", "entity2"],
      "contradicts_existing_id": null
    }
  ]
}

If no durable memory worth saving is found, return {"memories": []}.
"""


class MemoryExtractor:
    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        retriever: Optional[MemoryRetriever] = None,
        llm_runner: Optional[LLMRunner] = None,
        config: Optional[Config] = None,
    ):
        self.config = config or Config()
        self.store = store or MemoryStore(self.config)
        self.retriever = retriever or MemoryRetriever(self.store, self.config)
        self.llm_runner = llm_runner or LLMRunner(self.config)

    def extract_from_transcript(self, transcript_text: str, provider: Optional[str] = None) -> List[MemoryItem]:
        """Extract durable memories from transcript text and persist them."""
        if not transcript_text.strip():
            return []

        # Scrub secrets from transcript first
        clean_transcript = scrub_secrets(transcript_text[:15000])

        prompt = f"{EXTRACTION_SYSTEM_PROMPT}\n\nTRANSCRIPT:\n{clean_transcript}"
        success, response_text = self.llm_runner.run_prompt(prompt, provider=provider)

        extracted_items: List[MemoryItem] = []

        if success and response_text:
            extracted_items = self._parse_llm_json(response_text)

        if not extracted_items:
            # Deterministic fallback parsing if LLM output is empty or failed
            extracted_items = self._heuristic_fallback_extraction(clean_transcript)

        saved_items: List[MemoryItem] = []

        for item in extracted_items:
            # Double scrub
            item.content = scrub_secrets(item.content)
            if not item.content.strip():
                continue

            # Check if this item supersedes an old item
            if item.superseded_by:
                old_id = item.superseded_by
                item.superseded_by = None  # Clear temp field on new item
                self.store.update_status(old_id, MemoryStatus.SUPERSEDED, superseded_by=item.id)

            saved = self.store.save_item(item)
            saved_items.append(saved)

        if saved_items:
            self.retriever.reindex()

        return saved_items

    def _parse_llm_json(self, llm_output: str) -> List[MemoryItem]:
        """Parse structured JSON output from LLM response."""
        json_str = llm_output.strip()

        # Extract markdown json block if wrapped in ```json ... ```
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", json_str)
        if match:
            json_str = match.group(1)
        elif not json_str.startswith("{"):
            start = json_str.find("{")
            end = json_str.rfind("}")
            if start != -1 and end != -1:
                json_str = json_str[start : end + 1]

        try:
            data = json.loads(json_str)
            raw_memories = data.get("memories", [])
            items: List[MemoryItem] = []

            for raw in raw_memories:
                content = raw.get("content", "").strip()
                if not content:
                    continue

                conf = float(raw.get("confidence", 1.0))
                if conf < 0.6:
                    continue

                scope_str = raw.get("scope", "global").lower()
                scope = MemoryScope.PROJECT if scope_str == "project" else MemoryScope.GLOBAL

                type_str = raw.get("type", "preference").lower()
                try:
                    mtype = MemoryType(type_str)
                except ValueError:
                    mtype = MemoryType.PREFERENCE

                mem_id = generate_memory_id()
                curr_proj = self.store.project_dir.parent.name if self.store.project_dir else None

                item = MemoryItem(
                    id=mem_id,
                    content=content,
                    scope=scope,
                    project_id=curr_proj if scope == MemoryScope.PROJECT else None,
                    type=mtype,
                    status=MemoryStatus.ACTIVE,
                    confidence=conf,
                    tags=raw.get("tags", []),
                    entities=raw.get("entities", []),
                    superseded_by=raw.get("contradicts_existing_id"),
                )
                items.append(item)
            return items
        except Exception:
            return []

    def _heuristic_fallback_extraction(self, text: str) -> List[MemoryItem]:
        """Deterministic regex rule fallback if LLM is unavailable."""
        items: List[MemoryItem] = []
        patterns = [
            (r"(?i)(lembre-se|remember|always use|nunca use|always prefer)\s+([^\n.]+)", MemoryType.PREFERENCE),
            (r"(?i)(decisão|decision|decidido|decided)\s*:\s*([^\n.]+)", MemoryType.DECISION),
            (r"(?i)(erro|error|bug|solução|fix)\s*:\s*([^\n.]+)", MemoryType.LESSON),
        ]

        for pat, mtype in patterns:
            matches = re.findall(pat, text)
            for match in matches:
                content_str = match[1].strip() if isinstance(match, tuple) else match.strip()
                if len(content_str) < 10 or len(content_str) > 300:
                    continue
                mem_id = generate_memory_id()
                item = MemoryItem(
                    id=mem_id,
                    content=content_str,
                    scope=MemoryScope.GLOBAL,
                    type=mtype,
                    confidence=0.8,
                    tags=["heuristic-fallback"],
                )
                items.append(item)
                if len(items) >= 3:
                    break
        return items
