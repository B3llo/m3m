"""Durable local queue for asynchronous SessionEnd processing.

The lifecycle hook only writes a small, scrubbed JSON envelope to ``pending``.
LLM extraction and memory writes happen later through the explicit queue
processor. Each state transition is an atomic rename, so an interrupted hook
cannot leave a partially written job in the queue.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .sanitizer import scrub_secrets

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


QUEUE_STATES = ("pending", "processing", "failed", "done")
DEFAULT_LEASE_SECONDS = 300
WORKER_LOCK_NAME = ".worker.lock"
MAX_RAW_INPUT_CHARS = 200_000
MAX_STRING_CHARS = 20_000
MAX_LIST_ITEMS = 200
MAX_DICT_ITEMS = 200
MAX_NESTING = 6
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
HOOK_JOB_PREFIX = "hook_"
QUARANTINE_DIR_NAME = "quarantine"

_QUEUE_THREAD_LOCKS: Dict[str, threading.RLock] = {}
_QUEUE_THREAD_LOCKS_GUARD = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Optional[datetime] = None) -> str:
    return (value or _utc_now()).isoformat()


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


def _safe_value(value: Any, depth: int = 0) -> Any:
    """Recursively scrub and bound arbitrary hook JSON without trusting it."""
    if depth > MAX_NESTING:
        return "[TRUNCATED_NESTED_VALUE]"
    if isinstance(value, str):
        return scrub_secrets(value)[:MAX_STRING_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for index, (key, nested) in enumerate(value.items()):
            if index >= MAX_DICT_ITEMS:
                break
            safe[scrub_secrets(str(key))[:200]] = _safe_value(nested, depth + 1)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in list(value)[:MAX_LIST_ITEMS]]
    return scrub_secrets(str(value))[:MAX_STRING_CHARS]


@dataclass
class QueueEntry:
    """A serialized SessionEnd event and its processing state."""

    job_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    raw_input: str = ""
    session_id: Optional[str] = None
    source: str = "session-end"
    require_approval: bool = True
    created_at: str = field(default_factory=_timestamp)
    status: str = "pending"
    attempts: int = 0
    processing_started_at: Optional[str] = None
    completed_at: Optional[str] = None
    failed_at: Optional[str] = None
    last_error: str = ""
    owner_token: Optional[str] = None
    lease_expires_at: Optional[str] = None
    captured_by_hook: bool = False
    effects: Dict[str, Any] = field(default_factory=dict)
    retry_blocked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueEntry":
        if not isinstance(data, dict):
            raise ValueError("queue entry must be an object")
        job_id = str(data.get("job_id", "")).strip()
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("queue entry has invalid job_id")
        status = str(data.get("status", "pending"))
        if status not in QUEUE_STATES:
            raise ValueError(f"invalid queue state: {status}")
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("queue entry payload must be an object")
        try:
            attempts = max(0, int(data.get("attempts", 0)))
        except (TypeError, ValueError, OverflowError):
            attempts = 0
        return cls(
            job_id=job_id,
            payload=_safe_value(payload),
            raw_input=scrub_secrets(str(data.get("raw_input", "")))[:MAX_RAW_INPUT_CHARS],
            session_id=scrub_secrets(str(data["session_id"]))[:500] if data.get("session_id") is not None else None,
            source=scrub_secrets(str(data.get("source", "session-end")))[:200] or "session-end",
            require_approval=bool(data.get("require_approval", True)),
            created_at=scrub_secrets(str(data.get("created_at") or _timestamp()))[:100],
            status=status,
            attempts=attempts,
            processing_started_at=scrub_secrets(str(data.get("processing_started_at")))[:100]
            if data.get("processing_started_at") else None,
            completed_at=scrub_secrets(str(data.get("completed_at")))[:100]
            if data.get("completed_at") else None,
            failed_at=scrub_secrets(str(data.get("failed_at")))[:100]
            if data.get("failed_at") else None,
            last_error=scrub_secrets(str(data.get("last_error", "")))[:2000],
            owner_token=scrub_secrets(str(data.get("owner_token")))[:100]
            if data.get("owner_token") else None,
            lease_expires_at=scrub_secrets(str(data.get("lease_expires_at")))[:100]
            if data.get("lease_expires_at") else None,
            captured_by_hook=bool(data.get("captured_by_hook", False)),
            effects=_safe_value(data.get("effects", {}))
            if isinstance(data.get("effects", {}), dict)
            else {},
            retry_blocked=bool(data.get("retry_blocked", False)),
        )


@dataclass
class QueueProcessReport:
    processed: int = 0
    failed: int = 0
    retried: int = 0
    results: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class QueueError(RuntimeError):
    """Raised for queue configuration or serialization errors."""


class SessionQueue:
    """Filesystem-backed queue with atomic state transitions."""

    def __init__(
        self,
        config: Optional[Config] = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ):
        self.config = config or Config()
        self.lease_seconds = max(0.05, float(lease_seconds))
        self.last_errors: List[str] = []
        self.worker_token = f"worker_{uuid.uuid4().hex}"

    @property
    def queue_dir(self) -> Optional[Path]:
        if not self.config.project_dir:
            return None
        return self.config.project_dir / "sessions" / "queue"

    @contextmanager
    def worker_lock(self, blocking: bool = False):
        """Ensure at most one extraction worker per project at any time.

        Extraction shells out to a provider CLI which boots a whole process
        tree; letting every hook spawn its own worker piled up dozens of
        concurrent provider processes. The lock is advisory: cooperative
        callers that cannot acquire it skip work instead of stacking up.
        """
        acquired = False
        handle = None
        queue_dir = self._ensure_dirs()
        lock_key = f"worker:{queue_dir.resolve()}"
        with _QUEUE_THREAD_LOCKS_GUARD:
            thread_lock = _QUEUE_THREAD_LOCKS.setdefault(lock_key, threading.RLock())
        thread_lock.acquire()
        try:
            try:
                handle = (queue_dir / WORKER_LOCK_NAME).open("a+", encoding="utf-8")
            except OSError:
                handle = None
            if handle is None:
                yield False
                return
            if fcntl is None:
                acquired = True
            else:
                flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                try:
                    fcntl.flock(handle.fileno(), flags)
                    acquired = True
                except OSError:
                    acquired = False
            yield acquired
        finally:
            if handle is not None:
                if acquired and fcntl is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()
            thread_lock.release()

    def worker_lock_active(self) -> bool:
        """Non-blocking probe: is another extraction worker active right now?"""
        with self.worker_lock(blocking=False) as acquired:
            return not acquired

    def state_dir(self, state: str) -> Optional[Path]:
        if state not in QUEUE_STATES:
            raise ValueError(f"invalid queue state: {state}")
        return self.queue_dir / state if self.queue_dir else None

    def _ensure_dirs(self) -> Path:
        if not self.queue_dir:
            raise QueueError("no project memory directory is configured")
        for state in QUEUE_STATES:
            (self.queue_dir / state).mkdir(parents=True, exist_ok=True)
        (self.queue_dir / QUARANTINE_DIR_NAME).mkdir(parents=True, exist_ok=True)
        return self.queue_dir

    @contextmanager
    def _locked(self):
        """Serialize queue transitions across threads and cooperating processes."""
        queue_dir = self._ensure_dirs()
        lock_key = str(queue_dir.resolve())
        with _QUEUE_THREAD_LOCKS_GUARD:
            thread_lock = _QUEUE_THREAD_LOCKS.setdefault(lock_key, threading.RLock())

        thread_lock.acquire()
        handle = None
        try:
            lock_path = queue_dir / ".queue.lock"
            handle = lock_path.open("a+", encoding="utf-8")
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if handle is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()
            thread_lock.release()

    @staticmethod
    def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(encoded)
                handle.write(b"\n")
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

    @staticmethod
    def _read(path: Path) -> Optional[QueueEntry]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return QueueEntry.from_dict(data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _quarantine(self, path: Path, reason: str) -> None:
        """Move an untrusted or state-inconsistent file out of the queue."""
        if not path.exists() or not self.queue_dir:
            return
        quarantine_dir = self.queue_dir / QUARANTINE_DIR_NAME
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / f"{path.stem}_{uuid.uuid4().hex[:12]}.json"
        try:
            os.replace(path, target)
            self.last_errors.append(f"quarantined {path}: {reason}")
        except OSError:
            self.last_errors.append(f"invalid queue entry: {path} ({reason})")

    def _load_entry(
        self,
        path: Path,
        expected_state: Optional[str] = None,
        quarantine_invalid: bool = False,
    ) -> Optional[QueueEntry]:
        entry = self._read(path)
        if entry is None:
            if quarantine_invalid:
                self._quarantine(path, "invalid JSON or schema")
            return None
        if expected_state and entry.status != expected_state:
            if quarantine_invalid:
                self._quarantine(path, f"directory expects {expected_state}, entry says {entry.status}")
            return None
        if expected_state and path.stem != entry.job_id:
            if quarantine_invalid:
                self._quarantine(path, "filename does not match job_id")
            return None
        return entry

    def _list_entries_unlocked(self, state: str) -> List[QueueEntry]:
        directory = self.state_dir(state)
        if not directory or not directory.exists():
            return []
        entries: List[QueueEntry] = []
        for path in sorted(directory.glob("*.json")):
            entry = self._load_entry(path, expected_state=state, quarantine_invalid=True)
            if entry is not None:
                entries.append(entry)
        return entries

    def _resolve_requeue_duplicates_unlocked(self) -> int:
        """Finish an interrupted failed-to-pending retry transition.

        Requeue first commits a complete pending file and only then removes the
        failed source.  A crash between those two operations intentionally
        leaves two valid files.  The pending copy is the committed successor,
        so remove the stale failed copy without quarantining either job.
        """
        failed_dir = self.state_dir("failed")
        pending_dir = self.state_dir("pending")
        if not failed_dir or not pending_dir:
            return 0
        resolved = 0
        for failed_path in sorted(failed_dir.glob("*.json")):
            pending_path = pending_dir / failed_path.name
            if not pending_path.exists():
                continue
            failed = self._load_entry(failed_path, expected_state="failed")
            pending = self._load_entry(pending_path, expected_state="pending")
            if not failed or not pending or failed.job_id != pending.job_id:
                # Do not discard ambiguous files. They remain available for
                # explicit repair instead of being silently quarantined.
                self.last_errors.append(f"unresolved retry duplicate: {failed_path.name}")
                continue
            try:
                failed_path.unlink()
                resolved += 1
            except OSError:
                self.last_errors.append(f"could not finalize retry duplicate: {failed_path.name}")
        return resolved

    def _recover_claim_markers(self) -> int:
        """Recover jobs interrupted between the atomic claim and final rename."""
        processing_dir = self.state_dir("processing")
        if not processing_dir:
            return 0
        recovered = 0
        for marker in processing_dir.glob(".*.claiming"):
            entry = self._load_entry(marker, quarantine_invalid=True)
            if not entry:
                continue
            target = self._entry_path("pending", entry.job_id)
            if target.exists():
                self._quarantine(marker, "claim marker conflicts with pending job")
                continue
            if entry.status not in {"pending", "processing"}:
                self._quarantine(marker, f"claim marker has terminal state {entry.status}")
                continue
            entry.status = "pending"
            entry.owner_token = None
            entry.lease_expires_at = None
            entry.processing_started_at = None
            try:
                self._atomic_write(marker, entry.to_dict())
                os.replace(marker, target)
                recovered += 1
            except (OSError, TypeError, ValueError):
                continue
        return recovered

    def enqueue(
        self,
        payload: Optional[Dict[str, Any]] = None,
        raw_input: str = "",
        session_id: Optional[str] = None,
        source: str = "session-end",
        require_approval: bool = True,
        captured_by_hook: bool = False,
    ) -> Optional[QueueEntry]:
        """Sanitize and atomically enqueue an event without doing extraction."""
        self._ensure_dirs()
        safe_payload = _safe_value(payload or {})
        if not isinstance(safe_payload, dict):
            safe_payload = {}
        safe_raw_input = scrub_secrets(str(raw_input))[:MAX_RAW_INPUT_CHARS]
        safe_session_id = scrub_secrets(str(session_id))[:500] if session_id is not None else None
        entry = QueueEntry(
            # Hook-created jobs carry an immutable origin marker in the job ID.
            # The JSON fields below are retained for compatibility and
            # observability, but the worker must not trust them for policy.
            job_id=f"{HOOK_JOB_PREFIX if captured_by_hook else 'q_'}{_utc_now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:12]}",
            payload=safe_payload,
            raw_input=safe_raw_input,
            session_id=safe_session_id,
            source=scrub_secrets(str(source))[:200] or "session-end",
            require_approval=bool(require_approval),
            captured_by_hook=bool(captured_by_hook),
        )
        path = self.state_dir("pending") / f"{entry.job_id}.json"
        try:
            self._atomic_write(path, entry.to_dict())
        except (OSError, TypeError, ValueError) as exc:
            raise QueueError(f"could not enqueue SessionEnd event: {exc}")
        return entry

    @staticmethod
    def is_session_end_job(entry: QueueEntry) -> bool:
        """Return whether an entry belongs to the hook-only SessionEnd queue."""
        # ``captured_by_hook`` is legacy metadata and may be changed in the
        # persisted JSON. The hook prefix is part of the validated job ID and
        # therefore remains stable across ordinary payload tampering.
        return str(entry.job_id).startswith(HOOK_JOB_PREFIX)

    def list_entries(self, state: str = "pending") -> List[QueueEntry]:
        self.state_dir(state)  # validate state before taking the lock
        with self._locked():
            self.last_errors = []
            self._resolve_requeue_duplicates_unlocked()
            return self._list_entries_unlocked(state)

    def _entry_path(self, state: str, job_id: str) -> Path:
        directory = self.state_dir(state)
        if not directory:
            raise QueueError("no project memory directory is configured")
        if not JOB_ID_PATTERN.fullmatch(str(job_id)):
            raise QueueError("invalid queue job ID")
        return directory / f"{job_id}.json"

    def _recover_stale_processing(self) -> int:
        """Recover expired leases under the shared queue lock."""
        with self._locked():
            return self._recover_stale_processing_unlocked()

    def _recover_stale_processing_unlocked(self) -> int:
        recovered = 0
        now = _utc_now()
        for entry in self._list_entries_unlocked("processing"):
            started = _parse_timestamp(entry.processing_started_at)
            lease_expires = _parse_timestamp(entry.lease_expires_at)
            stale = (
                lease_expires <= now
                if lease_expires is not None
                else started is None or (now - started).total_seconds() >= self.lease_seconds
            )
            if not stale:
                continue
            source = self._entry_path("processing", entry.job_id)
            target = self._entry_path("pending", entry.job_id)
            if target.exists():
                self._quarantine(source, "stale job conflicts with pending job")
                continue
            original = entry.to_dict()
            try:
                entry.status = "pending"
                entry.owner_token = None
                entry.lease_expires_at = None
                entry.processing_started_at = None
                self._atomic_write(source, entry.to_dict())
                os.replace(source, target)
                recovered += 1
            except (OSError, TypeError, ValueError):
                try:
                    if source.exists():
                        self._atomic_write(source, original)
                except (OSError, TypeError, ValueError):
                    self._quarantine(source, "failed stale-lease recovery rollback")
                continue
        return recovered

    def claim_next(self, retry_failed: bool = False) -> Optional[QueueEntry]:
        """Atomically claim one pending event for processing."""
        with self._locked():
            self._resolve_requeue_duplicates_unlocked()
            self._recover_claim_markers()
            self._recover_stale_processing_unlocked()
            if retry_failed:
                self._requeue_failed_unlocked()

            for entry in self._list_entries_unlocked("pending"):
                source = self._entry_path("pending", entry.job_id)
                target = self._entry_path("processing", entry.job_id)
                marker = target.parent / f".{entry.job_id}.{self.worker_token}.claiming"
                current = self._load_entry(source, expected_state="pending", quarantine_invalid=True)
                if current is None:
                    continue
                if target.exists():
                    self._quarantine(source, "pending job already has a processing file")
                    continue
                try:
                    os.replace(source, marker)
                except OSError:
                    continue
                claimed = self._load_entry(marker, quarantine_invalid=True)
                if claimed is None or claimed.status != "pending":
                    if marker.exists():
                        self._quarantine(marker, "claim source was not pending")
                    continue
                claimed.status = "processing"
                claimed.attempts += 1
                claimed.processing_started_at = _timestamp()
                claimed.owner_token = self.worker_token
                claimed.lease_expires_at = _timestamp(_utc_now() + timedelta(seconds=self.lease_seconds))
                try:
                    self._atomic_write(marker, claimed.to_dict())
                    os.replace(marker, target)
                    return claimed
                except (OSError, TypeError, ValueError):
                    try:
                        claimed.status = "pending"
                        claimed.owner_token = None
                        claimed.lease_expires_at = None
                        claimed.processing_started_at = None
                        self._atomic_write(marker, claimed.to_dict())
                        os.replace(marker, source)
                    except (OSError, TypeError, ValueError):
                        self._quarantine(marker, "claim rollback failed")
        return None

    def renew(self, job_id: str, owner_token: Optional[str] = None) -> bool:
        """Renew a live lease without allowing another worker to take ownership."""
        if not owner_token:
            return False
        with self._locked():
            processing_path = self._entry_path("processing", job_id)
            entry = self._load_entry(processing_path, expected_state="processing", quarantine_invalid=True)
            if not entry or entry.owner_token != owner_token:
                return False
            entry.lease_expires_at = _timestamp(
                _utc_now() + timedelta(seconds=self.lease_seconds)
            )
            try:
                self._atomic_write(processing_path, entry.to_dict())
                return True
            except (OSError, TypeError, ValueError):
                return False

    def _heartbeat(self, job_id: str, owner_token: str, stop: threading.Event) -> None:
        interval = max(0.01, self.lease_seconds / 3)
        while not stop.wait(interval):
            if not self.renew(job_id, owner_token):
                return

    def complete(self, job_id: str, owner_token: Optional[str] = None) -> bool:
        """Mark a processing entry done; repeated calls are harmless."""
        if not owner_token:
            return False
        with self._locked():
            done_path = self._entry_path("done", job_id)
            if done_path.is_file():
                done_entry = self._load_entry(done_path, expected_state="done", quarantine_invalid=True)
                return bool(done_entry and done_entry.owner_token == owner_token)
            processing_path = self._entry_path("processing", job_id)
            entry = self._load_entry(processing_path, expected_state="processing", quarantine_invalid=True)
            if entry is None or entry.owner_token != owner_token:
                return False
            original = entry.to_dict()
            entry.status = "done"
            entry.completed_at = _timestamp()
            entry.processing_started_at = None
            entry.lease_expires_at = None
            try:
                self._atomic_write(processing_path, entry.to_dict())
                os.replace(processing_path, done_path)
            except (OSError, TypeError, ValueError):
                try:
                    if processing_path.exists():
                        self._atomic_write(processing_path, original)
                except (OSError, TypeError, ValueError):
                    # Leave the original processing file in place. A later
                    # lease recovery can retry the transition; moving it to
                    # quarantine here would turn a transient write failure
                    # into an avoidable lost/retry-blocked job.
                    pass
                return False
            return True

    def fail(
        self,
        job_id: str,
        error: str,
        owner_token: Optional[str] = None,
        retry_blocked: bool = False,
    ) -> bool:
        """Move a failed event to failed while retaining its original payload."""
        if not owner_token:
            return False
        with self._locked():
            failed_path = self._entry_path("failed", job_id)
            if failed_path.is_file():
                failed_entry = self._load_entry(failed_path, expected_state="failed", quarantine_invalid=True)
                return bool(failed_entry and failed_entry.owner_token == owner_token)
            processing_path = self._entry_path("processing", job_id)
            entry = self._load_entry(processing_path, expected_state="processing", quarantine_invalid=True)
            if entry is None or entry.owner_token != owner_token:
                return False
            original = entry.to_dict()
            entry.status = "failed"
            entry.failed_at = _timestamp()
            entry.processing_started_at = None
            entry.lease_expires_at = None
            entry.last_error = scrub_secrets(str(error))[:2000]
            entry.retry_blocked = bool(retry_blocked)
            try:
                self._atomic_write(processing_path, entry.to_dict())
                os.replace(processing_path, failed_path)
            except (OSError, TypeError, ValueError):
                try:
                    if processing_path.exists():
                        self._atomic_write(processing_path, original)
                except (OSError, TypeError, ValueError):
                    # Preserve the processing file for stale-lease recovery.
                    pass
                return False
            return True

    def record_effects(
        self,
        job_id: str,
        owner_token: Optional[str],
        effects: Dict[str, Any],
    ) -> bool:
        """Checkpoint side effects while a job is owned by this worker."""
        if not owner_token or not isinstance(effects, dict):
            return False
        with self._locked():
            path = self._entry_path("processing", job_id)
            entry = self._load_entry(path, expected_state="processing", quarantine_invalid=True)
            if entry is None or entry.owner_token != owner_token:
                return False
            current = dict(entry.effects)
            safe_effects = _safe_value(effects)
            if isinstance(safe_effects, dict):
                current.update(safe_effects)
            entry.effects = current
            try:
                self._atomic_write(path, entry.to_dict())
                return True
            except (OSError, TypeError, ValueError):
                return False

    def requeue_failed(self, job_id: Optional[str] = None) -> int:
        """Return failed events to pending, preserving attempts and payloads."""
        with self._locked():
            return self._requeue_failed_unlocked(job_id)

    def _requeue_failed_unlocked(self, job_id: Optional[str] = None) -> int:
        self._resolve_requeue_duplicates_unlocked()
        entries = self._list_entries_unlocked("failed")
        count = 0
        for entry in entries:
            if job_id and entry.job_id != job_id:
                continue
            if entry.retry_blocked:
                self.last_errors.append(f"retry blocked for job {entry.job_id}: side effects are not checkpointed")
                continue
            source = self._entry_path("failed", entry.job_id)
            target = self._entry_path("pending", entry.job_id)
            if target.exists():
                self._quarantine(source, "failed job conflicts with pending job")
                continue
            entry.status = "pending"
            entry.failed_at = None
            entry.processing_started_at = None
            entry.owner_token = None
            entry.lease_expires_at = None
            entry.last_error = ""
            try:
                # Commit the successor before deleting the failed source. A
                # crash can leave both files, which is deterministically
                # resolved by _resolve_requeue_duplicates_unlocked().
                self._atomic_write(target, entry.to_dict())
                source.unlink()
                count += 1
            except (OSError, TypeError, ValueError):
                # If the pending successor was committed but source removal
                # failed, leave both valid records for deterministic recovery.
                # If the first write failed, the original failed job is intact.
                continue
        return count

    @staticmethod
    def _retry_requires_reconciliation(effects: Any) -> bool:
        """Only an explicitly confirmed partial effect can block a retry."""
        if not isinstance(effects, dict):
            return False
        confirmed = any(
            effects.get(key) is True
            for key in ("memory_committed", "handoff_committed", "committed")
        )
        return confirmed and effects.get("reconciliation_required") is True

    def process(
        self,
        processor: Callable[[QueueEntry], Any],
        limit: Optional[int] = 10,
        retry_failed: bool = False,
    ) -> QueueProcessReport:
        """Process jobs explicitly and retain failed jobs for later retry."""
        report = QueueProcessReport()
        if retry_failed:
            report.retried = self.requeue_failed()
        remaining = None if limit is None else max(0, int(limit))
        while remaining is None or report.processed + report.failed < remaining:
            try:
                entry = self.claim_next()
            except (OSError, TypeError, ValueError, QueueError) as exc:
                report.failed += 1
                report.results.append({"status": "failed", "error": scrub_secrets(str(exc))[:2000]})
                break
            if entry is None:
                break

            # A completed effect checkpoint makes retry safe even if the final
            # queue rename failed. Never invoke a side-effecting processor twice
            # when the durable checkpoint says it already ran.
            if entry.effects.get("committed") is True:
                completed = self.complete(entry.job_id, owner_token=entry.owner_token)
                if completed:
                    report.processed += 1
                    report.results.append({
                        "job_id": entry.job_id,
                        "status": "done",
                        "result": entry.effects.get("result", {"replayed": True}),
                    })
                else:
                    self.fail(
                        entry.job_id,
                        "could not finalize checkpointed queue entry",
                        owner_token=entry.owner_token,
                    )
                    report.failed += 1
                    report.results.append({
                        "job_id": entry.job_id,
                        "status": "failed",
                        "error": "could not finalize checkpointed queue entry",
                    })
                continue

            stop_heartbeat = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat,
                args=(entry.job_id, entry.owner_token or "", stop_heartbeat),
                daemon=True,
            )
            heartbeat.start()
            try:
                result = processor(entry)
            except Exception as exc:
                # The processor may have checkpointed an effect immediately
                # before raising. Re-read the owned file instead of relying on
                # the stale in-memory QueueEntry when deciding retry safety.
                persisted_effects = False
                with self._locked():
                    processing_path = self._entry_path("processing", entry.job_id)
                    persisted = self._load_entry(
                        processing_path,
                        expected_state="processing",
                        quarantine_invalid=True,
                    )
                    persisted_effects = dict(persisted.effects) if persisted else {}
                self.fail(
                    entry.job_id,
                    str(exc),
                    owner_token=entry.owner_token,
                    retry_blocked=self._retry_requires_reconciliation(persisted_effects),
                )
                report.failed += 1
                report.results.append({"job_id": entry.job_id, "status": "failed", "error": scrub_secrets(str(exc))[:2000]})
                continue
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=max(0.1, self.lease_seconds))

            # Record a final effect checkpoint before the queue state changes.
            # If complete() fails afterwards, the next retry skips the processor.
            result_for_storage = _safe_value(result)
            effect_checkpoint = {"committed": True, "result": result_for_storage}
            if isinstance(result_for_storage, dict) and isinstance(result_for_storage.get("effects"), dict):
                effect_checkpoint.update(result_for_storage["effects"])
            checkpointed = self.record_effects(
                entry.job_id,
                entry.owner_token,
                effect_checkpoint,
            )
            if not checkpointed:
                self.fail(
                    entry.job_id,
                    "could not persist side-effect checkpoint",
                    owner_token=entry.owner_token,
                    retry_blocked=self._retry_requires_reconciliation(entry.effects),
                )
                report.failed += 1
                report.results.append({
                    "job_id": entry.job_id,
                    "status": "failed",
                    "error": "could not persist side-effect checkpoint",
                })
                continue

            try:
                completed = self.complete(entry.job_id, owner_token=entry.owner_token)
            except (OSError, TypeError, ValueError):
                completed = False
            if not completed:
                # The checkpoint is already durable, so a retry is safe. If the
                # failure path itself cannot move the job, no worker may safely
                # process it again until an operator inspects the queue.
                self.fail(entry.job_id, "could not mark queue entry done", owner_token=entry.owner_token)
                report.failed += 1
                report.results.append({
                    "job_id": entry.job_id,
                    "status": "failed",
                    "error": "could not mark queue entry done",
                })
                continue
            report.processed += 1
            report.results.append({"job_id": entry.job_id, "status": "done", "result": result})
        return report

    def counts(self) -> Dict[str, int]:
        return {state: len(self.list_entries(state)) for state in QUEUE_STATES}


# Public aliases keep the storage abstraction discoverable for callers that
# prefer the ``QueueStore`` name.
QueueStore = SessionQueue
QueueJob = QueueEntry
