"""Doctor: System diagnostics and health checker for agent-m3m."""

import os
import shutil
import sqlite3
from pathlib import Path
from typing import List, Tuple
from .config import Config
from .store import MemoryStore
from .sanitizer import has_secrets


class Doctor:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.store = MemoryStore(self.config)

    def run_diagnostics(self) -> List[Tuple[str, str, bool]]:
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
        db_path = self.config.global_dir / ".index" / "memory_index.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM memory_meta;")
                count = c.fetchone()[0]
                conn.close()
                results.append(("FTS5 Index", f"Index DB is healthy ({count} memories indexed)", True))
            except Exception as e:
                results.append(("FTS5 Index", f"Index DB error: {e}", False))
        else:
            results.append(("FTS5 Index", "FTS5 Index DB not built yet (will build on search)", True))

        # 5. CLI Provider Check
        prov = self.config.active_provider
        prov_binary = prov if prov != "codex" else "codex"
        if prov == "claude":
            prov_binary = "claude"
        elif prov == "opencode":
            prov_binary = "opencode"
        elif prov == "kimi":
            prov_binary = "kimi"

        binary_path = shutil.which(prov_binary)
        if binary_path:
            results.append(("CLI Provider", f"Provider '{prov}' binary found at {binary_path}", True))
        else:
            results.append(("CLI Provider", f"Provider '{prov}' binary not found in PATH", False))

        # 6. Secret Scrubber Test
        test_secret = "sk-proj-1234567890abcdef1234567890abcdef"
        scrubber_ok = has_secrets(test_secret)
        results.append(("Secret Sanitizer", "Regex secret patterns loaded and operational", scrubber_ok))

        return results
