"""Configuration management for agent-m3m."""

import os
import hashlib
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

from .models import normalize_topic


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
        self.api_base_url = "https://api.z.ai/api/coding/paas/v4/chat/completions"
        self.api_key_env = "ZAI_API_KEY"
        self.api_key = ""
        
        self.top_candidates = 20
        self.top_results = 5
        self.llm_rerank = False
        self.topic_seeds: list[str] = []
        self.secret_scrubbing = True
        self.max_core_lines = 50
        self.max_context_chars = 12000
        self.max_topic_page_chars = 1200
        self.max_transcript_chars = 15000
        self.provider_timeout_seconds = 120
        self.hook_timeout_seconds = 3
        self.local_only = False
        self.auto_approve_confidence = 0.9
        self.export_universal_max_lines = 150
        
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

    @property
    def project_root(self) -> Optional[Path]:
        """Return the repository/workspace root represented by project_dir."""
        return self.project_dir.parent.resolve() if self.project_dir else None

    @property
    def project_id(self) -> Optional[str]:
        """Return a stable, path-derived project identifier for index isolation."""
        root = self.project_root
        if not root:
            return None
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
        return f"{root.name}-{digest}"

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
            self.max_context_chars = gen.get("max_context_chars", self.max_context_chars)
            self.max_topic_page_chars = gen.get("max_topic_page_chars", self.max_topic_page_chars)
            self.max_transcript_chars = gen.get("max_transcript_chars", self.max_transcript_chars)
            self.provider_timeout_seconds = gen.get("provider_timeout_seconds", self.provider_timeout_seconds)
            self.hook_timeout_seconds = gen.get("hook_timeout_seconds", self.hook_timeout_seconds)
            self.local_only = gen.get("local_only", self.local_only)
            try:
                self.auto_approve_confidence = max(0.0, min(1.0, float(
                    gen.get("auto_approve_confidence", self.auto_approve_confidence)
                )))
            except (TypeError, ValueError):
                pass
            try:
                self.export_universal_max_lines = max(20, int(
                    gen.get("export_universal_max_lines", self.export_universal_max_lines)
                ))
            except (TypeError, ValueError):
                pass
            
            prov = data.get("providers", {})
            self.active_provider = prov.get("active_provider", self.active_provider)
            self.model_name = prov.get("model_name", self.model_name)
            self.reasoning_effort = prov.get("reasoning_effort", self.reasoning_effort)
            self.codex_command = prov.get("codex_command", self.codex_command)
            self.claude_command = prov.get("claude_command", self.claude_command)
            self.opencode_command = prov.get("opencode_command", self.opencode_command)
            self.kimi_command = prov.get("kimi_command", self.kimi_command)
            self.api_base_url = prov.get("api_base_url", self.api_base_url)
            self.api_key_env = prov.get("api_key_env", self.api_key_env)
            self.api_key = prov.get("api_key", self.api_key)
            
            ret = data.get("retrieval", {})
            self.top_candidates = ret.get("top_candidates", self.top_candidates)
            self.top_results = ret.get("top_results", self.top_results)
            self.llm_rerank = ret.get("llm_rerank", self.llm_rerank)

            topics = data.get("topics", {})
            raw_seeds = topics.get("seeds")
            if isinstance(raw_seeds, list):
                normalized = []
                for seed in raw_seeds:
                    slug = normalize_topic(seed)
                    if slug and slug not in normalized:
                        normalized.append(slug)
                self.topic_seeds = normalized
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
                elif val.startswith("[") and val.endswith("]"):
                    try:
                        parsed = json.loads(val)
                        v = parsed if isinstance(parsed, list) else val
                    except ValueError:
                        v = [item.strip().strip("\"'") for item in val[1:-1].split(",") if item.strip()]
                elif val.startswith('"') and val.endswith('"'):
                    v = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    v = val[1:-1]
                else:
                    try:
                        v = int(val)
                    except ValueError:
                        try:
                            v = float(val)
                        except ValueError:
                            v = val
                data[current_section][key] = v
        return data

    def save_config(self):
        """Save config parameters to config.toml."""
        self.global_dir.mkdir(parents=True, exist_ok=True)
        def toml_string(value: str) -> str:
            return json.dumps(value, ensure_ascii=False)

        toml_content = (
            "# agent-m3m configuration file\n\n"
            "[general]\n"
            f"secret_scrubbing = {str(self.secret_scrubbing).lower()}\n"
            f"max_core_lines = {self.max_core_lines}\n\n"
            f"max_context_chars = {self.max_context_chars}\n"
            f"max_topic_page_chars = {self.max_topic_page_chars}\n"
            f"max_transcript_chars = {self.max_transcript_chars}\n"
            f"provider_timeout_seconds = {self.provider_timeout_seconds}\n"
            f"hook_timeout_seconds = {self.hook_timeout_seconds}\n"
            f"local_only = {str(self.local_only).lower()}\n"
            f"auto_approve_confidence = {float(self.auto_approve_confidence):.2f}\n"
            f"export_universal_max_lines = {self.export_universal_max_lines}\n"
            "\n"
            "[providers]\n"
            f"active_provider = {toml_string(self.active_provider)}\n"
            f"model_name = {toml_string(self.model_name)}\n"
            f"reasoning_effort = {toml_string(self.reasoning_effort)}\n"
            f"codex_command = {toml_string(self.codex_command)}\n"
            f"claude_command = {toml_string(self.claude_command)}\n"
            f"opencode_command = {toml_string(self.opencode_command)}\n"
            f"kimi_command = {toml_string(self.kimi_command)}\n"
            f"api_base_url = {toml_string(self.api_base_url)}\n"
            f"api_key_env = {toml_string(self.api_key_env)}\n"
            f"api_key = {toml_string(self.api_key)}\n\n"
            "[retrieval]\n"
            f"top_candidates = {self.top_candidates}\n"
            f"top_results = {self.top_results}\n"
            f"llm_rerank = {str(self.llm_rerank).lower()}\n"
            "\n"
            "[topics]\n"
            f"seeds = {json.dumps(self.topic_seeds, ensure_ascii=False)}\n"
        )
        temp_file = self.config_file.with_suffix(".toml.tmp")
        temp_file.write_text(toml_content, encoding="utf-8")
        os.replace(temp_file, self.config_file)

    def save_default_config(self):
        """Save a default config.toml if missing."""
        if not self.config_file.exists():
            self.save_config()
