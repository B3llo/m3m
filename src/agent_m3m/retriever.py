"""SQLite FTS5 retrieval with isolated, disposable per-project indexes."""

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .models import MemoryItem, MemoryScope, MemoryStatus, MemoryType
from .sanitizer import scrub_secrets
from .store import MemoryStore


STOPWORDS = {
    "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos", "e", "em", "na", "nas",
    "no", "nos", "o", "os", "ou", "para", "por", "que", "se", "um", "uma", "umas", "uns",
    "eu", "voce", "você", "ele", "ela", "nos", "nós", "eles", "elas", "meu", "minha", "seu",
    "sua", "esse", "essa", "isso", "este", "esta", "isto", "aquele", "aquela", "aquilo",
    "como", "quando", "onde", "qual", "quais", "quem", "fazer", "faça", "faca", "usar", "use",
    "quero", "pode", "projeto", "agente",
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "is", "are",
    "was", "were", "be", "this", "that", "it", "you", "i", "me", "my", "we", "our", "all",
    "can", "do", "does", "did", "please", "make", "use", "using", "project", "agent", "from",
}


class ScoredCandidate:
    def __init__(self, item: MemoryItem, total_score: float, breakdown: Dict[str, float]):
        self.item = item
        self.total_score = total_score
        self.breakdown = breakdown


class MemoryRetriever:
    # Bump when the index schema changes so existing databases rebuild.
    INDEX_SCHEMA_VERSION = "3"

    def __init__(self, store: Optional[MemoryStore] = None, config: Optional[Config] = None):
        self.config = config or Config()
        self.store = store or MemoryStore(self.config)
        self.db_path = self._index_path()
        self._ensure_db_dir()

    def _index_path(self) -> Path:
        # The global store is shared, but project memories must never leak into
        # another repository's index.
        key = self.config.project_id or "global"
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", key)
        return self.config.global_dir / ".index" / f"memory_index_{safe_key}.db"

    def _ensure_db_dir(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _connection(self, path: Optional[Path] = None):
        conn = sqlite3.connect(str(path or self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _get_connection(self, path: Optional[Path] = None) -> sqlite3.Connection:
        """Legacy helper for direct connection access."""
        conn = sqlite3.connect(str(path or self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _source_signature(self) -> str:
        """Hash memory paths and metadata so manual edits invalidate the index."""
        parts: List[str] = []
        roots = [self.config.global_dir]
        if self.config.project_dir and self.config.project_dir.resolve() != self.config.global_dir.resolve():
            roots.append(self.config.project_dir)
        for root in roots:
            if not root.exists():
                continue
            for subdir in MemoryStore.SUBDIRS:
                folder = root / subdir
                if not folder.exists():
                    continue
                for path in sorted(folder.glob("*.md")):
                    try:
                        stat = path.stat()
                        parts.append(f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}")
                    except OSError:
                        continue
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _is_current(self) -> bool:
        if not self.db_path.exists():
            return False
        try:
            with self._connection() as conn:
                row = conn.execute("SELECT value FROM index_state WHERE key = 'signature'").fetchone()
                version = conn.execute("SELECT value FROM index_state WHERE key = 'schema_version'").fetchone()
                return bool(
                    row
                    and row["value"] == self._source_signature()
                    and version
                    and version["value"] == self.INDEX_SCHEMA_VERSION
                )
        except sqlite3.Error:
            return False

    def reindex(self) -> int:
        """Build a fresh SQLite index and replace the old one atomically."""
        self._ensure_db_dir()
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{self.db_path.name}.", suffix=".tmp", dir=str(self.db_path.parent), delete=False
            ) as handle:
                temp_path = Path(handle.name)

            with self._connection(temp_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA journal_mode=DELETE")
                cursor.execute(
                    """
                    CREATE VIRTUAL TABLE memory_fts USING fts5(
                        id UNINDEXED,
                        content,
                        tags,
                        entities,
                        topic,
                        tokenize='porter unicode61'
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE memory_meta (
                        id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL,
                        project_id TEXT,
                        type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        topic TEXT NOT NULL DEFAULT '',
                        created TEXT,
                        updated TEXT,
                        file_path TEXT,
                        source TEXT,
                        source_session TEXT,
                        evidence_json TEXT,
                        expires_at TEXT,
                        last_used TEXT,
                        use_count INTEGER NOT NULL DEFAULT 0,
                        corroborated_count INTEGER NOT NULL DEFAULT 0,
                        last_corroborated TEXT,
                        pinned INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                cursor.execute("CREATE TABLE index_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

                items = self.store.list_items(include_expired=True)
                indexed_count = 0
                for item in items:
                    scope = item.scope.value if isinstance(item.scope, MemoryScope) else str(item.scope)
                    mtype = item.type.value if isinstance(item.type, MemoryType) else str(item.type)
                    status = item.status.value if isinstance(item.status, MemoryStatus) else str(item.status)
                    topic = str(getattr(item, "topic", "") or "")
                    cursor.execute(
                        "INSERT INTO memory_fts (id, content, tags, entities, topic) VALUES (?, ?, ?, ?, ?)",
                        (item.id, item.content, " ".join(item.tags), " ".join(item.entities), topic),
                    )
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO memory_meta
                        (id, scope, project_id, type, status, confidence, topic, created, updated,
                         file_path, source, source_session, evidence_json, expires_at, last_used, use_count, corroborated_count, last_corroborated, pinned)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.id,
                            scope,
                            item.project_id,
                            mtype,
                            status,
                            item.confidence,
                            topic,
                            item.created,
                            item.updated,
                            item.file_path,
                            item.source,
                            item.source_session,
                            json.dumps(item.evidence, ensure_ascii=False),
                            item.expires_at,
                            item.last_used,
                            item.use_count,
                            int(getattr(item, "corroborated_count", 0) or 0),
                            getattr(item, "last_corroborated", None),
                            1 if item.pinned else 0,
                        ),
                    )
                    indexed_count += 1

                cursor.execute("INSERT INTO index_state (key, value) VALUES ('signature', ?)", (self._source_signature(),))
                cursor.execute(
                    "INSERT INTO index_state (key, value) VALUES ('schema_version', ?)",
                    (self.INDEX_SCHEMA_VERSION,),
                )
                conn.commit()

            os.replace(temp_path, self.db_path)
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass
            temp_path = None
            return indexed_count
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _safe_fts_query(query: str) -> Tuple[str, List[str]]:
        """Extract meaningful quoted tokens and build safe FTS5 query."""
        raw_tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        clean_tokens = [token[:80] for token in raw_tokens if token.strip()]
        meaningful = [token for token in clean_tokens if len(token) > 1 and token.lower() not in STOPWORDS]
        active_tokens = meaningful[:16] if meaningful else clean_tokens[:16]
        if not active_tokens:
            return "", []
        fts_query = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in active_tokens)
        return fts_query, active_tokens

    @staticmethod
    def _or_fts_query(tokens: List[str]) -> str:
        """Build OR query from tokens for progressive/fallback matching."""
        if not tokens:
            return ""
        return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:16])

    @staticmethod
    def _enum_or_default(enum_type, value: Any, default):
        try:
            return enum_type(value)
        except (TypeError, ValueError):
            return default

    def _fetch_rows(
        self,
        fts_query: str,
        limit: int,
        scope: Optional[str],
        status: Optional[str],
    ) -> List[sqlite3.Row]:
        with self._connection() as conn:
            sql = """
                SELECT f.id, f.content, f.tags, f.entities, bm25(memory_fts) AS bm25_score,
                       m.scope, m.project_id, m.type, m.status, m.confidence, m.topic, m.created,
                       m.updated, m.file_path, m.source, m.source_session, m.evidence_json,
                       m.expires_at, m.last_used, m.use_count, m.corroborated_count, m.last_corroborated, m.pinned
                FROM memory_fts f
                JOIN memory_meta m ON f.id = m.id
                WHERE memory_fts MATCH ?
            """
            params: List[Any] = [fts_query]
            if status:
                sql += " AND m.status = ?"
                params.append(status)
            if scope:
                sql += " AND m.scope = ?"
                params.append(scope)
            sql += " ORDER BY bm25(memory_fts) LIMIT ?"
            params.append(limit)
            return conn.execute(sql, params).fetchall()

    def search(
        self,
        query: str,
        top_k: int = 5,
        scope: Optional[str] = None,
        status: Optional[str] = "active",
        debug: bool = False,
    ) -> List[ScoredCandidate]:
        """Search memories using safe FTS5 multi-tier matching plus transparent metadata ranking."""
        fts_query, query_tokens = self._safe_fts_query(query)
        if not fts_query:
            return []

        scope_value = scope.value if isinstance(scope, MemoryScope) else scope
        status_value = status.value if isinstance(status, MemoryStatus) else status
        if not self._is_current():
            self.reindex()

        candidate_limit = max(int(top_k), int(self.config.top_candidates), 1)
        try:
            rows = self._fetch_rows(fts_query, candidate_limit, scope_value, status_value)
        except sqlite3.OperationalError:
            self.reindex()
            rows = self._fetch_rows(fts_query, candidate_limit, scope_value, status_value)

        # Multi-tier fallback for long prompts: if strict AND matched fewer than candidate_limit, supplement with OR query
        if len(rows) < candidate_limit and query_tokens:
            or_query = self._or_fts_query(query_tokens)
            if or_query and or_query != fts_query:
                try:
                    or_rows = self._fetch_rows(or_query, candidate_limit, scope_value, status_value)
                    seen_ids = {row["id"] for row in rows}
                    for or_row in or_rows:
                        if or_row["id"] not in seen_ids:
                            rows.append(or_row)
                            seen_ids.add(or_row["id"])
                except sqlite3.OperationalError:
                    pass

        if not rows:
            return []

        bm25_scores = [float(row["bm25_score"]) for row in rows]
        min_bm25 = min(bm25_scores)
        max_bm25 = max(bm25_scores)
        query_words = {word.lower() for word in query_tokens}
        current_project = self.config.project_id
        candidates: List[ScoredCandidate] = []
        seen_normalized_contents: set = set()

        for row in rows:
            # Enforce project isolation: project-scoped items from other projects must not leak
            row_scope = str(row["scope"])
            row_proj = row["project_id"]
            if row_scope == MemoryScope.PROJECT.value and row_proj and current_project and row_proj != current_project:
                continue

            item_status = self._enum_or_default(MemoryStatus, row["status"], MemoryStatus.ACTIVE)
            item_topic = str(row["topic"] or "")
            item = MemoryItem(
                id=row["id"],
                content=row["content"],
                scope=self._enum_or_default(MemoryScope, row["scope"], MemoryScope.GLOBAL),
                project_id=row["project_id"],
                type=self._enum_or_default(MemoryType, row["type"], MemoryType.PREFERENCE),
                status=item_status,
                confidence=float(row["confidence"] or 0.0),
                topic=item_topic,
                tags=(row["tags"] or "").split(),
                entities=(row["entities"] or "").split(),
                created=row["created"] or "",
                updated=row["updated"] or "",
                source=row["source"] or "index",
                source_session=row["source_session"],
                evidence=json.loads(row["evidence_json"] or "[]"),
                expires_at=row["expires_at"],
                last_used=row["last_used"],
                use_count=int(row["use_count"] or 0),
                corroborated_count=int(row["corroborated_count"] or 0) if "corroborated_count" in row.keys() else 0,
                last_corroborated=row["last_corroborated"] if "last_corroborated" in row.keys() else None,
                pinned=bool(row["pinned"]) if "pinned" in row.keys() else False,
                file_path=row["file_path"],
            )
            if item.is_expired():
                continue

            # Deduplication by content
            norm_content = " ".join(item.content.lower().split())
            if norm_content in seen_normalized_contents:
                continue
            seen_normalized_contents.add(norm_content)

            raw_bm25 = float(row["bm25_score"])
            norm_lexical = 1.0 if max_bm25 == min_bm25 else (max_bm25 - raw_bm25) / (max_bm25 - min_bm25 + 1e-6)
            tags_list = [tag.lower() for tag in item.tags]
            entities_list = [entity.lower() for entity in item.entities]
            tag_bonus = 0.10 if any(tag in query_words for tag in tags_list) else 0.0
            entity_bonus = 0.15 if any(entity in query_words for entity in entities_list) else 0.0
            project_bonus = 0.15 if (
                row["scope"] == MemoryScope.PROJECT.value and
                (row["project_id"] in {None, current_project})
            ) else 0.0
            status_bonus = 0.20 if item_status == MemoryStatus.ACTIVE else 0.0
            recency_bonus = 0.0
            try:
                updated = datetime.fromisoformat((row["updated"] or "").replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age_days = max((datetime.now(timezone.utc) - updated).total_seconds() / 86400, 0)
                recency_bonus = 0.05 * max(0.0, 1.0 - age_days / 30.0)
            except ValueError:
                pass
            # Reinforcement signals: repeatedly injected memories and pinned
            # memories earn a bounded bonus on top of lexical relevance.
            # Corroboration (independent re-observation across sessions) uses
            # the same curve as usage so repeated rules outrank singletons.
            is_pinned = bool(row["pinned"]) if "pinned" in row.keys() else False
            use_bonus = min(0.15, 0.05 * math.log1p(max(0, int(row["use_count"] or 0))))
            corroborated_raw = 0
            if "corroborated_count" in row.keys():
                try:
                    corroborated_raw = max(0, int(row["corroborated_count"] or 0))
                except (TypeError, ValueError):
                    corroborated_raw = 0
            corroboration_bonus = min(0.15, 0.05 * math.log1p(corroborated_raw))
            pin_bonus = 0.25 if is_pinned else 0.0
            # Topic match: when the prompt names a topic (e.g. "storefront" or
            # "nightly ops" against topic "nightly-ops"), that topic's memories rank up.
            topic_tokens = {token for token in re.findall(r"[a-z0-9]+", item_topic) if token}
            topic_bonus = 0.0
            if topic_tokens:
                query_lower = query.lower()
                if (
                    topic_tokens & query_words
                    or item_topic in query_lower
                    or all(token in query_lower for token in topic_tokens)
                ):
                    topic_bonus = 0.20

            total_score = (
                norm_lexical * 0.40 + tag_bonus + entity_bonus + project_bonus
                + status_bonus + recency_bonus + use_bonus + corroboration_bonus + pin_bonus + topic_bonus
            )
            breakdown = {
                "lexical": round(norm_lexical * 0.40, 3),
                "entity_bonus": round(entity_bonus, 3),
                "tag_bonus": round(tag_bonus, 3),
                "project_bonus": round(project_bonus, 3),
                "status_bonus": round(status_bonus, 3),
                "recency_bonus": round(recency_bonus, 3),
                "use_bonus": round(use_bonus, 3),
                "corroboration_bonus": round(corroboration_bonus, 3),
                "pin_bonus": round(pin_bonus, 3),
                "topic_bonus": round(topic_bonus, 3),
            }
            candidates.append(ScoredCandidate(item, round(total_score, 3), breakdown))

        candidates.sort(key=lambda candidate: candidate.total_score, reverse=True)
        if self.config.llm_rerank and len(candidates) > 1:
            rerank_limit = max(int(self.config.top_candidates), 1)
            candidates = self._llm_rerank(query, candidates[:rerank_limit]) + candidates[rerank_limit:]
        return candidates[: max(int(top_k), 1)]

    def _llm_rerank(self, query: str, candidates: List[ScoredCandidate]) -> List[ScoredCandidate]:
        """Optional provider reranking, disabled by default and fail-open."""
        from .llm_runner import LLMRunner

        compact = [
            {"id": item.item.id, "content": scrub_secrets(item.item.content)[:1000]}
            for item in candidates
        ]
        prompt = (
            "Return only a JSON array of memory IDs ordered by relevance to the query. "
            f"Query: {scrub_secrets(query)[:500]}\nCandidates: {json.dumps(compact, ensure_ascii=False)}"
        )
        success, response = LLMRunner(self.config).run_prompt(
            prompt,
            output_schema={"type": "array", "items": {"type": "string"}},
        )
        if not success:
            return candidates
        try:
            ordered_ids = json.loads(response)
            if not isinstance(ordered_ids, list):
                return candidates
            by_id = {candidate.item.id: candidate for candidate in candidates}
            ordered = [by_id[item_id] for item_id in ordered_ids if item_id in by_id]
            ordered.extend(candidate for candidate in candidates if candidate not in ordered)
            return ordered
        except (ValueError, TypeError):
            return candidates
