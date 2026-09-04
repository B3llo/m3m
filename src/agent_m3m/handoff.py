"""Small local handoff store for carrying work between agent sessions."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Iterator, List, Optional

from .config import Config
from .sanitizer import scrub_secrets


@dataclass
class Handoff:
    session_id: str
    agent: str = "unknown"
    project_id: Optional[str] = None
    summary: str = ""
    decisions: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    handoff_id: str = field(default_factory=lambda: f"handoff_{uuid.uuid4().hex[:16]}")
    expires_at: Optional[str] = None
    consumed_at: Optional[str] = None


class HandoffStore:
    DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60

    def __init__(self, config: Optional[Config] = None, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.config = config or Config()
        self.ttl_seconds = max(1, int(ttl_seconds))

    @property
    def handoff_dir(self) -> Optional[Path]:
        return self.config.project_dir / "sessions" if self.config.project_dir else None

    @property
    def handoff_path(self) -> Optional[Path]:
        return self.handoff_dir / "pending_handoff.json" if self.handoff_dir else None

    @property
    def handoff_items_dir(self) -> Optional[Path]:
        return self.handoff_dir / "handoffs" if self.handoff_dir else None

    @property
    def lock_path(self) -> Optional[Path]:
        return self.handoff_dir / "pending_handoff.lock" if self.handoff_dir else None

    @staticmethod
    def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None

    def _expires_at(self, created_at: str, supplied: Optional[str]) -> str:
        if supplied and self._parse_timestamp(supplied):
            return str(supplied)
        created = self._parse_timestamp(created_at) or datetime.now(timezone.utc)
        return (created + timedelta(seconds=self.ttl_seconds)).isoformat()

    @staticmethod
    def _legacy_handoff_id(data: dict) -> str:
        raw = f"{data.get('session_id', 'unknown')}|{data.get('created_at', '')}"
        return f"handoff_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _safe_handoff_id(value: object) -> str:
        candidate = scrub_secrets(str(value or ""))[:200]
        if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", candidate):
            return candidate
        return f"handoff_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(path.parent), prefix=".handoff.", suffix=".tmp", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize handoff save/consume transitions when fcntl is available."""
        lock_path = self.lock_path
        if not lock_path:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        try:
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            if handle:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                handle.close()

    def save(self, handoff: Handoff, idempotency_key: Optional[str] = None) -> Optional[Path]:
        if not self.handoff_path or not self.handoff_items_dir:
            return None
        self.handoff_items_dir.mkdir(parents=True, exist_ok=True)
        created_at = self._parse_timestamp(handoff.created_at)
        normalized_created_at = (created_at or datetime.now(timezone.utc)).isoformat()
        safe = Handoff(
            session_id=scrub_secrets(handoff.session_id)[:500],
            agent=scrub_secrets(handoff.agent)[:200],
            project_id=scrub_secrets(handoff.project_id or "")[:500] or None,
            summary=scrub_secrets(handoff.summary)[:4000],
            decisions=[scrub_secrets(str(value))[:500] for value in handoff.decisions[:20]],
            files=[scrub_secrets(str(value))[:500] for value in handoff.files[:50]],
            pending=[scrub_secrets(str(value))[:500] for value in handoff.pending[:20]],
            next_steps=[scrub_secrets(str(value))[:500] for value in handoff.next_steps[:20]],
            created_at=normalized_created_at,
            handoff_id=self._safe_handoff_id(handoff.handoff_id),
            expires_at=self._expires_at(normalized_created_at, handoff.expires_at),
            consumed_at=scrub_secrets(handoff.consumed_at)[:100] if handoff.consumed_at else None,
        )

        if idempotency_key:
            digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:24]
            safe.handoff_id = f"handoff_job_{digest}"

        item_path = self.handoff_items_dir / f"{safe.handoff_id}.json"
        pointer_path = self.handoff_path
        with self._locked():
            self._atomic_write(item_path, asdict(safe))
            self._atomic_write(
                pointer_path,
                {"handoff_id": safe.handoff_id, "updated_at": datetime.now(timezone.utc).isoformat()},
            )
        return item_path

    def _from_data(self, data: dict) -> Optional[Handoff]:
        if not isinstance(data, dict) or "session_id" not in data:
            return None
        try:
            handoff = Handoff(
                session_id=scrub_secrets(str(data.get("session_id", "unknown")))[:500],
                agent=scrub_secrets(str(data.get("agent", "unknown")))[:200],
                project_id=scrub_secrets(str(data.get("project_id")))[:500] if data.get("project_id") else None,
                summary=scrub_secrets(str(data.get("summary", "")))[:4000],
                decisions=self._values(data.get("decisions", [])),
                files=self._values(data.get("files", [])),
                pending=self._values(data.get("pending", [])),
                next_steps=self._values(data.get("next_steps", [])),
                created_at=scrub_secrets(str(data.get("created_at", "")))[:100],
                handoff_id=self._safe_handoff_id(data.get("handoff_id") or self._legacy_handoff_id(data)),
                expires_at=scrub_secrets(str(data.get("expires_at")))[:100]
                if data.get("expires_at") else self._expires_at(str(data.get("created_at", "")), None),
                consumed_at=scrub_secrets(str(data.get("consumed_at")))[:100]
                if data.get("consumed_at") else None,
            )
            return handoff
        except (ValueError, TypeError, OverflowError):
            return None

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _load_file(self, path: Path) -> Optional[Handoff]:
        data = self._read_json(path)
        return self._from_data(data) if data else None

    def _pointer_id(self) -> Optional[str]:
        pointer = self._read_json(self.handoff_path) if self.handoff_path and self.handoff_path.is_file() else None
        if not pointer or "session_id" in pointer:
            return None
        value = pointer.get("handoff_id")
        return value if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", str(value or "")) else None

    def _item_path(self, handoff_id: str) -> Optional[Path]:
        safe_id = self._safe_handoff_id(handoff_id)
        if safe_id != handoff_id or not self.handoff_items_dir:
            return None
        return self.handoff_items_dir / f"{safe_id}.json"

    def _iter_items(self) -> List[Handoff]:
        if not self.handoff_items_dir or not self.handoff_items_dir.is_dir():
            return []
        items = []
        for path in self.handoff_items_dir.glob("*.json"):
            handoff = self._load_file(path)
            if handoff:
                items.append(handoff)
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def load(
        self,
        include_expired: bool = False,
        include_consumed: bool = False,
        handoff_id: Optional[str] = None,
    ) -> Optional[Handoff]:
        """Load one selected handoff without losing older pending items."""
        candidates: List[Handoff] = []
        if handoff_id:
            item_path = self._item_path(handoff_id)
            if item_path and item_path.is_file():
                item = self._load_file(item_path)
                if item:
                    candidates.append(item)
            elif self.handoff_path and self.handoff_path.is_file():
                legacy = self._load_file(self.handoff_path)
                if legacy and legacy.handoff_id == handoff_id:
                    candidates.append(legacy)
        else:
            pointer_id = self._pointer_id()
            if pointer_id:
                item_path = self._item_path(pointer_id)
                if item_path and item_path.is_file():
                    item = self._load_file(item_path)
                    if item:
                        candidates.append(item)
            candidates.extend(self._iter_items())
            if self.handoff_path and self.handoff_path.is_file():
                legacy = self._load_file(self.handoff_path)
                if legacy:
                    candidates.append(legacy)

        seen = set()
        for handoff in candidates:
            if handoff.handoff_id in seen:
                continue
            seen.add(handoff.handoff_id)
            if handoff.consumed_at and not include_consumed:
                continue
            if self.is_expired(handoff) and not include_expired:
                continue
            return handoff
        return None

    @staticmethod
    def _values(value) -> List[str]:
        if isinstance(value, list):
            return [scrub_secrets(str(item))[:500] for item in value[:50]]
        if value:
            return [scrub_secrets(str(value))[:500]]
        return []

    def is_expired(self, handoff: Handoff, now: Optional[datetime] = None) -> bool:
        expires = self._parse_timestamp(handoff.expires_at)
        if not expires:
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc) >= expires

    def consume(self, handoff_id: Optional[str] = None, confirmed: bool = False) -> bool:
        """Acknowledge a handoff only after confirmation, idempotently."""
        with self._locked():
            handoff = self.load(include_expired=True, include_consumed=True, handoff_id=handoff_id)
            if not handoff:
                return False
            if handoff.consumed_at:
                return True
            if not confirmed or self.is_expired(handoff):
                return False
            handoff.consumed_at = datetime.now(timezone.utc).isoformat()
            path = self._item_path(handoff.handoff_id)
            if not path or not path.is_file():
                # Preserve compatibility with the old single-file format.
                path = self.handoff_path
            if not path or not path.is_file():
                return False
            self._atomic_write(path, asdict(handoff))
            return True

    def acknowledge(self, handoff_id: Optional[str] = None) -> bool:
        """Explicitly acknowledge the currently displayed handoff."""
        return self.consume(handoff_id=handoff_id, confirmed=True)

    def clear(self, confirmed: bool = False) -> bool:
        """Backward-compatible alias that requires explicit confirmation.

        The file is retained with ``consumed_at`` for idempotency and auditability.
        """
        return self.consume(confirmed=confirmed)
