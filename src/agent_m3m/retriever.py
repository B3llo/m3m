"""MemoryRetriever: SQLite FTS5 search index and ranking engine for agent-m3m."""

import math
from pathlib import Path
import sqlite3
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone

from .models import MemoryItem, MemoryStatus, MemoryScope
from .store import MemoryStore
from .config import Config


class ScoredCandidate:
    def __init__(
        self,
        item: MemoryItem,
        total_score: float,
        breakdown: Dict[str, float],
    ):
        self.item = item
        self.total_score = total_score
        self.breakdown = breakdown


class MemoryRetriever:
    def __init__(self, store: Optional[MemoryStore] = None, config: Optional[Config] = None):
        self.config = config or Config()
        self.store = store or MemoryStore(self.config)
        self.db_path = self.config.global_dir / ".index" / "memory_index.db"
        self._ensure_db_dir()

    def _ensure_db_dir(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def reindex(self) -> int:
        """Rebuild SQLite FTS5 index completely from Markdown files (SSOT)."""
        self._ensure_db_dir()
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("DROP TABLE IF EXISTS memory_fts;")
        cursor.execute("DROP TABLE IF EXISTS memory_meta;")

        cursor.execute(
            """
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                id UNINDEXED,
                content,
                tags,
                entities,
                tokenize='porter ascii'
            );
            """
        )

        cursor.execute(
            """
            CREATE TABLE memory_meta (
                id TEXT PRIMARY KEY,
                scope TEXT,
                project_id TEXT,
                type TEXT,
                status TEXT,
                confidence REAL,
                created TEXT,
                updated TEXT,
                file_path TEXT
            );
            """
        )

        items = self.store.list_items()
        indexed_count = 0

        for item in items:
            tags_str = " ".join(item.tags)
            entities_str = " ".join(item.entities)

            cursor.execute(
                "INSERT INTO memory_fts (id, content, tags, entities) VALUES (?, ?, ?, ?);",
                (item.id, item.content, tags_str, entities_str),
            )
            cursor.execute(
                """
                INSERT INTO memory_meta (id, scope, project_id, type, status, confidence, created, updated, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    item.id,
                    item.scope.value if isinstance(item.scope, MemoryScope) else item.scope,
                    item.project_id,
                    item.type.value if hasattr(item.type, "value") else str(item.type),
                    item.status.value if isinstance(item.status, MemoryStatus) else item.status,
                    item.confidence,
                    item.created,
                    item.updated,
                    item.file_path,
                ),
            )
            indexed_count += 1

        conn.commit()
        conn.close()
        return indexed_count

    def search(
        self,
        query: str,
        top_k: int = 5,
        scope: Optional[str] = None,
        status: Optional[str] = "active",
        debug: bool = False,
    ) -> List[ScoredCandidate]:
        """Search memories using BM25 FTS5 + metadata ranking."""
        if not self.db_path.exists():
            self.reindex()

        conn = self._get_connection()
        cursor = conn.cursor()

        # Sanitize query for FTS5
        clean_query = " ".join([w for w in query.replace('"', " ").replace("'", " ").split() if w])
        if not clean_query:
            conn.close()
            return []

        # FTS query with BM25 score
        fts_sql = """
            SELECT 
                f.id,
                f.content,
                f.tags,
                f.entities,
                bm25(memory_fts) AS bm25_score,
                m.scope,
                m.project_id,
                m.type,
                m.status,
                m.confidence,
                m.created,
                m.updated,
                m.file_path
            FROM memory_fts f
            JOIN memory_meta m ON f.id = m.id
            WHERE memory_fts MATCH ?
        """
        params = [clean_query]

        if status:
            fts_sql += " AND m.status = ?"
            params.append(status)

        try:
            cursor.execute(fts_sql, params)
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Reindex if table structure issue
            self.reindex()
            conn.close()
            return []

        conn.close()

        if not rows:
            return []

        # Find min/max BM25 for normalization (BM25 returns negative numbers in SQLite, lower = better match)
        bm25_scores = [r["bm25_score"] for r in rows]
        min_bm25 = min(bm25_scores)
        max_bm25 = max(bm25_scores)

        query_words = set(clean_query.lower().split())
        candidates: List[ScoredCandidate] = []

        curr_project = self.config.project_dir.parent.name if self.config.project_dir else None

        for r in rows:
            raw_bm25 = r["bm25_score"]
            # Normalize BM25 to range 0.0 to 1.0 (where 1.0 is best)
            if max_bm25 == min_bm25:
                norm_lexical = 1.0
            else:
                norm_lexical = (max_bm25 - raw_bm25) / (max_bm25 - min_bm25 + 1e-6)

            # Metadata bonuses
            tags_list = (r["tags"] or "").split()
            entities_list = (r["entities"] or "").split()

            tag_match = any(t.lower() in query_words for t in tags_list)
            entity_match = any(e.lower() in query_words for e in entities_list)

            tag_bonus = 0.10 if tag_match else 0.0
            entity_bonus = 0.15 if entity_match else 0.0

            # Project scope bonus
            proj_bonus = 0.15 if (r["scope"] == "project" or r["project_id"] == curr_project) else 0.0

            # Status bonus
            status_bonus = 0.20 if r["status"] == "active" else 0.0

            # Recency bonus (last 7 days)
            recency_bonus = 0.0
            try:
                dt = datetime.fromisoformat(r["updated"].replace("Z", "+00:00"))
                days_old = (datetime.now(timezone.utc) - dt).days
                if days_old <= 7:
                    recency_bonus = 0.05
            except Exception:
                pass

            total_score = norm_lexical * 0.40 + tag_bonus + entity_bonus + proj_bonus + status_bonus + recency_bonus

            item = MemoryItem(
                id=r["id"],
                content=r["content"],
                scope=r["scope"],
                project_id=r["project_id"],
                type=r["type"],
                status=r["status"],
                confidence=r["confidence"],
                tags=tags_list,
                entities=entities_list,
                created=r["created"],
                updated=r["updated"],
                file_path=r["file_path"],
            )

            breakdown = {
                "lexical": round(norm_lexical * 0.40, 3),
                "entity_bonus": round(entity_bonus, 3),
                "tag_bonus": round(tag_bonus, 3),
                "project_bonus": round(proj_bonus, 3),
                "status_bonus": round(status_bonus, 3),
                "recency_bonus": round(recency_bonus, 3),
            }

            candidates.append(ScoredCandidate(item=item, total_score=round(total_score, 3), breakdown=breakdown))

        # Sort candidates descending by total_score
        candidates.sort(key=lambda c: c.total_score, reverse=True)
        return candidates[:top_k]
