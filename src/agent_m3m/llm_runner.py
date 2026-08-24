"""LLMRunner: Non-interactive provider runner for invoking Codex, Claude, OpenCode, Kimi, etc."""

import subprocess
import shutil
from typing import Optional, Tuple
from .config import Config


class LLMRunner:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()

    def run_prompt(self, prompt: str, provider: Optional[str] = None) -> Tuple[bool, str]:
        """Run non-interactive prompt using specified or default active provider."""
        prov = provider or self.config.active_provider

        if prov == "codex":
            return self._run_codex(prompt)
        elif prov == "claude":
            return self._run_claude(prompt)
        elif prov == "opencode":
            return self._run_opencode(prompt)
        elif prov == "kimi":
            return self._run_kimi(prompt)
        else:
            # Fallback to codex
            return self._run_codex(prompt)

    def _run_codex(self, prompt: str) -> Tuple[bool, str]:
        """Invoke Codex CLI non-interactively with configured model and effort."""
        if not shutil.which("codex"):
            return False, "Codex CLI binary not found in PATH"

        model = self.config.model_name or "luna"
        effort = self.config.reasoning_effort or "low"

        cmd = [
            "codex",
            "exec",
            "-c",
            f'model="{model}"',
            "-c",
            f'reasoning_effort="{effort}"',
            prompt,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                return True, res.stdout.strip()
            return False, res.stderr.strip() or res.stdout.strip()
        except Exception as e:
            return False, str(e)

    def _run_claude(self, prompt: str) -> Tuple[bool, str]:
        """Invoke Claude Code CLI non-interactively with configured model."""
        if not shutil.which("claude"):
            return False, "Claude Code binary not found in PATH"

        model = self.config.model_name or "haiku"

        cmd = [
            "claude",
            "-p",
            "--model",
            model,
            prompt,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                return True, res.stdout.strip()
            return False, res.stderr.strip() or res.stdout.strip()
        except Exception as e:
            return False, str(e)

    def _run_opencode(self, prompt: str) -> Tuple[bool, str]:
        """Invoke OpenCode CLI non-interactively."""
        if not shutil.which("opencode"):
            return False, "OpenCode binary not found in PATH"

        cmd = ["opencode", "run", prompt]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                return True, res.stdout.strip()
            return False, res.stderr.strip() or res.stdout.strip()
        except Exception as e:
            return False, str(e)

    def _run_kimi(self, prompt: str) -> Tuple[bool, str]:
        """Invoke Kimi CLI non-interactively."""
        if not shutil.which("kimi"):
            return False, "Kimi binary not found in PATH"

        cmd = ["kimi", "-p", prompt]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                return True, res.stdout.strip()
            return False, res.stderr.strip() or res.stdout.strip()
        except Exception as e:
            return False, str(e)
