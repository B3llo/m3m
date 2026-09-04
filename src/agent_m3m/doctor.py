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

        if verify_provider:
            provider_ok, provider_message = LLMRunner(self.config).verify_provider(live=True)
            results.append(("Provider Live Check", provider_message, provider_ok))

        return results
