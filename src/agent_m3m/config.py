"""Configuration management for agent-m3m."""

import os
from pathlib import Path
from typing import Dict, Any, Optional


class Config:
    def __init__(
        self,
        global_path: Optional[Path] = None,
        project_path: Optional[Path] = None,
    ):
        self.home_dir = Path.home().resolve()
        self.global_dir = global_path or (self.home_dir / ".agent-m3m")
        self.project_dir = project_path or self._find_project_dir()
        
        self.active_provider = "codex"
        self.model_name = "luna"
        self.reasoning_effort = "low"
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
        """Check current working directory and parent directories for .agent-m3m, excluding home dir."""
        cwd = Path.cwd().resolve()
        current = cwd

        # Stop search if we reach home directory or root
        while current != current.parent:
            if current == self.home_dir:
                break
            candidate = current / ".agent-m3m"
            if candidate.is_dir():
                return candidate
            if (current / ".git").is_dir():
                return current / ".agent-m3m"
            current = current.parent

        if cwd != self.home_dir:
            return cwd / ".agent-m3m"
        return None

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
            self.model_name = prov.get("model_name", self.model_name)
            self.reasoning_effort = prov.get("reasoning_effort", self.reasoning_effort)
            self.codex_command = prov.get("codex_command", self.codex_command)
            self.claude_command = prov.get("claude_command", self.claude_command)
            self.opencode_command = prov.get("opencode_command", self.opencode_command)
            self.kimi_command = prov.get("kimi_command", self.kimi_command)
            
            ret = data.get("retrieval", {})
            self.top_candidates = ret.get("top_candidates", self.top_candidates)
            self.top_results = ret.get("top_results", self.top_results)
            self.llm_rerank = ret.get("llm_rerank", self.llm_rerank)
        except Exception:
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

    def save_config(self):
        """Save config parameters to config.toml."""
        self.global_dir.mkdir(parents=True, exist_ok=True)
        toml_content = (
            "# agent-m3m configuration file\n\n"
            "[general]\n"
            f"secret_scrubbing = {str(self.secret_scrubbing).lower()}\n"
            f"max_core_lines = {self.max_core_lines}\n\n"
            "[providers]\n"
            f'active_provider = "{self.active_provider}"\n'
            f'model_name = "{self.model_name}"\n'
            f'reasoning_effort = "{self.reasoning_effort}"\n'
            f'codex_command = "{self.codex_command}"\n'
            f'claude_command = "{self.claude_command}"\n'
            f'opencode_command = "{self.opencode_command}"\n'
            f'kimi_command = "{self.kimi_command}"\n\n'
            "[retrieval]\n"
            f"top_candidates = {self.top_candidates}\n"
            f"top_results = {self.top_results}\n"
            f"llm_rerank = {str(self.llm_rerank).lower()}\n"
        )
        with open(self.config_file, "w", encoding="utf-8") as f:
            f.write(toml_content)

    def save_default_config(self):
        """Save a default config.toml if missing."""
        if not self.config_file.exists():
            self.save_config()
