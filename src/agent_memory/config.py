"""Configuration management for agent-memory."""

import os
from pathlib import Path
from typing import Dict, Any, Optional

try:
    import tomllib  # Python 3.11+
except ImportError:
    import json as tomllib  # Fallback for simpler format parser if needed


class Config:
    def __init__(
        self,
        global_path: Optional[Path] = None,
        project_path: Optional[Path] = None,
    ):
        self.home_dir = Path.home()
        self.global_dir = global_path or (self.home_dir / ".agent-memory")
        self.project_dir = project_path or self._find_project_dir()
        
        self.active_provider = "codex"
        self.codex_command = "codex exec"
        self.claude_command = "claude -p"
        self.opencode_command = "opencode run"
        self.kimi_command = "kimi"
        
        self.top_candidates = 20
        self.top_results = 5
        self.llm_rerank = False
        self.secret_scrubbing = True
        self.max_core_lines = 50
        
        self.load()

    def _find_project_dir(self) -> Optional[Path]:
        """Check current working directory and parent directories for .agent-memory."""
        cwd = Path.cwd().resolve()
        current = cwd
        while current != current.parent:
            candidate = current / ".agent-memory"
            if candidate.is_dir():
                return candidate
            if (current / ".git").is_dir():
                # Root of git repo
                return current / ".agent-memory"
            current = current.parent
        return cwd / ".agent-memory"

    @property
    def config_file(self) -> Path:
        return self.global_dir / "config.toml"

    def load(self):
        """Load configuration from config.toml if it exists."""
        if not self.config_file.is_file():
            return

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                content = f.read()
                data = self._parse_simple_toml(content)
                
            gen = data.get("general", {})
            self.secret_scrubbing = gen.get("secret_scrubbing", self.secret_scrubbing)
            self.max_core_lines = gen.get("max_core_lines", self.max_core_lines)
            
            prov = data.get("providers", {})
            self.active_provider = prov.get("active_provider", self.active_provider)
            self.codex_command = prov.get("codex_command", self.codex_command)
            self.claude_command = prov.get("claude_command", self.claude_command)
            self.opencode_command = prov.get("opencode_command", self.opencode_command)
            self.kimi_command = prov.get("kimi_command", self.kimi_command)
            
            ret = data.get("retrieval", {})
            self.top_candidates = ret.get("top_candidates", self.top_candidates)
            self.top_results = ret.get("top_results", self.top_results)
            self.llm_rerank = ret.get("llm_rerank", self.llm_rerank)
        except Exception as e:
            # Fallback to default settings
            pass

    def _parse_simple_toml(self, content: str) -> Dict[str, Any]:
        """Simple line-based TOML parser for basic key-value and sections."""
        data: Dict[str, Dict[str, Any]] = {}
        current_section = "general"
        data[current_section] = {}

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1].strip()
                if current_section not in data:
                    data[current_section] = {}
                continue

            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if val.lower() == "true":
                    v = True
                elif val.lower() == "false":
                    v = False
                elif val.startswith('"') and val.endswith('"'):
                    v = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    v = val[1:-1]
                else:
                    try:
                        v = int(val)
                    except ValueError:
                        v = val
                data[current_section][key] = v
        return data

    def save_default_config(self):
        """Save a default config.toml if missing."""
        self.global_dir.mkdir(parents=True, exist_ok=True)
        if self.config_file.exists():
            return

        default_toml = (
            "# agent-memory configuration file\n\n"
            "[general]\n"
            "secret_scrubbing = true\n"
            "max_core_lines = 50\n\n"
            "[providers]\n"
            'active_provider = "codex"\n'
            'codex_command = "codex exec"\n'
            'claude_command = "claude -p"\n'
            'opencode_command = "opencode run"\n'
            'kimi_command = "kimi"\n\n'
            "[retrieval]\n"
            "top_candidates = 20\n"
            "top_results = 5\n"
            "llm_rerank = false\n"
        )
        with open(self.config_file, "w", encoding="utf-8") as f:
            f.write(default_toml)
