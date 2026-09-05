"""Doctor: System diagnostics and health checker for agent-m3m."""

import os
import shutil
import shlex
import sqlite3
from pathlib import Path
from typing import List, Tuple, Optional
from .config import Config
from .models import MemoryStatus
from .store import MemoryStore
from .sanitizer import has_secrets
from .retriever import MemoryRetriever
from .llm_runner import LLMRunner


class Doctor:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.store = MemoryStore(self.config)

    def run_diagnostics(self, verify_provider: bool = False) -> List[Tuple[str, str, bool]]:
        """Run diagnostic checks and return list of (check_name, status_message, is_ok)."""
        results: List[Tuple[str, str, bool]] = []

        # 1. Global Directory
        global_ok = self.config.global_dir.exists()
        msg = f"Found at {self.config.global_dir}" if global_ok else f"Missing at {self.config.global_dir}"
        results.append(("Global Storage", msg, global_ok))

        # 2. Config File
        config_ok = self.config.config_file.is_file()
        msg = f"Found at {self.config.config_file}" if config_ok else f"Missing config.toml at {self.config.config_file}"
        results.append(("Config File", msg, config_ok))

        # 3. Project Directory
        proj_dir = self.config.project_dir
        proj_ok = proj_dir and proj_dir.exists()
        msg = f"Active at {proj_dir}" if proj_ok else "No local project .agent-m3m directory found"
        results.append(("Project Storage", msg, proj_ok))

        # 4. SQLite FTS5 Index
        retriever = MemoryRetriever(self.store, self.config)
        db_path = retriever.db_path
        if db_path.exists():
            conn = None
            try:
                conn = sqlite3.connect(str(db_path))
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM memory_meta;")
                count = c.fetchone()[0]
                current = retriever._is_current()
                # A stale index is disposable and self-heals on the next
                # search, so it stays a passing check.
                suffix = " and current" if current else " (refreshes automatically on the next search)"
                results.append(("FTS5 Index", f"Index DB is healthy ({count} memories indexed){suffix}", True))
            except Exception as e:
                results.append(("FTS5 Index", f"Index DB error: {e}", False))
            finally:
                if conn is not None:
                    conn.close()
        else:
            results.append(("FTS5 Index", "FTS5 Index DB not built yet (will build on search)", True))

        # 5. CLI Provider Check
        prov = self.config.active_provider
        if prov == "api":
            endpoint = (self.config.api_base_url or "").strip()
            key_ok = bool(LLMRunner(self.config).resolve_api_key())
            if endpoint and key_ok:
                results.append(("CLI Provider", f"Provider 'api' endpoint configured with a resolvable key ({endpoint})", True))
            elif endpoint:
                results.append(("CLI Provider", "Provider 'api' has no resolvable key (set api_key_env variable or api_key in config)", False))
            else:
                results.append(("CLI Provider", "Provider 'api' requires api_base_url configuration", False))
        else:
            try:
                configured = getattr(self.config, f"{prov}_command", prov)
                prov_binary = shlex.split(configured)[0]
            except (IndexError, ValueError):
                prov_binary = prov

            binary_path = shutil.which(prov_binary)
            if binary_path:
                results.append(("CLI Provider", f"Provider '{prov}' binary found at {binary_path}", True))
            else:
                results.append(("CLI Provider", f"Provider '{prov}' binary not found in PATH", False))

        # 6. Secret Scrubber Test
        test_secret = "sk-proj-1234567890abcdef1234567890abcdef"
        scrubber_ok = has_secrets(test_secret)
        results.append(("Secret Sanitizer", "Regex secret patterns loaded and operational", scrubber_ok))

        raw_secret_count = 0
        for item in self.store.list_items(include_expired=True):
            if item.file_path:
                try:
                    raw_secret_count += int(has_secrets(Path(item.file_path).read_text(encoding="utf-8")))
                except OSError:
                    pass
        results.append((
            "Stored Memory Scan",
            "No recognized secrets found in Markdown files" if raw_secret_count == 0 else f"Found {raw_secret_count} files containing recognized secret patterns",
            raw_secret_count == 0,
        ))

        # 7. Pending approval backlog
        try:
            pending_count = len(self.store.list_items(status=MemoryStatus.PENDING))
        except Exception:
            pending_count = 0
        results.append((
            "Pending Backlog",
            (
                f"{pending_count} pending memor{'y' if pending_count == 1 else 'ies'} awaiting "
                "`m3m approve` (review them or run `m3m approve --all`)"
                if pending_count
                else "No pending memory backlog"
            ),
            pending_count == 0,
        ))

        # 8. Config permissions (POSIX): config holds api_key material.
        try:
            cfg_path = self.config.config_file
            if cfg_path.is_file() and os.name == "posix":
                mode = cfg_path.stat().st_mode & 0o777
                if mode & 0o077:
                    results.append((
                        "Config Permissions",
                        f"{cfg_path} is mode {oct(mode)} (run chmod 600 {cfg_path})",
                        False,
                    ))
                else:
                    results.append(("Config Permissions", f"{cfg_path} is owner-only ({oct(mode)})", True))
            else:
                results.append(("Config Permissions", "No config file to check" if not cfg_path.is_file() else "Permission check skipped (non-POSIX)", True))
        except OSError:
            results.append(("Config Permissions", "Could not stat config file", True))

        # 9. Log hygiene (warn-only thresholds): hook logs grow unbounded.
        try:
            log_paths = []
            for base in {self.config.global_dir, self.config.project_dir}:
                if base:
                    log_paths.append(base / "sessions" / "m3m_hooks.log")
                    log_paths.append(base / "sessions" / "memory-save-diagnostics.jsonl")
            # Legacy default location when global_dir differs (kept for visibility).
            legacy = Path.home() / ".agent-m3m" / "sessions"
            log_paths.append(legacy / "m3m_hooks.log")
            log_paths.append(legacy / "memory-save-diagnostics.jsonl")
            total_bytes = 0
            biggest = None
            seen = set()
            for path in log_paths:
                try:
                    resolved = path.resolve()
                except OSError:
                    resolved = path
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    if path.is_file():
                        size = path.stat().st_size
                        total_bytes += size
                        if biggest is None or size > biggest[1]:
                            biggest = (path, size)
                except OSError:
                    continue
            if total_bytes > 10 * 1024 * 1024:
                where = f"{biggest[0]} ({biggest[1] / 1024 / 1024:.1f}MB)" if biggest else "hook logs"
                results.append((
                    "Log Hygiene",
                    f"Hook logs total {total_bytes / 1024 / 1024:.1f}MB; largest {where} (prune with `m3m clean --prune-logs --apply`)",
                    False,
                ))
            else:
                results.append((
                    "Log Hygiene",
                    f"Hook logs total {total_bytes / 1024:.0f}KB (prune threshold 10MB)",
                    True,
                ))
        except Exception:
            results.append(("Log Hygiene", "Could not measure hook logs", True))

        # 10. Queue hygiene (warn-only thresholds): done/failed retain forever.
        try:
            queue_base = self.config.project_dir / "sessions" / "queue" if self.config.project_dir else None
            if queue_base and queue_base.exists():
                counts = {}
                total = 0
                for state in ("pending", "processing", "failed", "done", "quarantine"):
                    try:
                        n = len(list((queue_base / state).glob("*.json")))
                    except OSError:
                        n = 0
                    counts[state] = n
                    total += n
                failed = counts.get("failed", 0)
                done = counts.get("done", 0)
                if failed > 20 or done > 200 or total > 500:
                    results.append((
                        "Queue Hygiene",
                        f"Queue has pending={counts.get('pending', 0)} failed={failed} done={done} total={total} "
                        "(recover with `m3m queue process --retry-failed`; prune done with `m3m clean --prune-queue --apply`)",
                        False,
                    ))
                else:
                    results.append((
                        "Queue Hygiene",
                        f"Queue healthy (pending={counts.get('pending', 0)} failed={failed} done={done})",
                        True,
                    ))
            else:
                results.append(("Queue Hygiene", "No project queue directory to check", True))
        except Exception:
            results.append(("Queue Hygiene", "Could not measure queue directories", True))

        if verify_provider:
            provider_ok, provider_message = LLMRunner(self.config).verify_provider(live=True)
            results.append(("Provider Live Check", provider_message, provider_ok))

        return results
