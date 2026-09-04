"""Non-interactive, configured provider runner for structured extraction."""

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from .config import Config
from .sanitizer import scrub_secrets


class LLMRunner:
    HEALTH_CHECK_PROMPT = (
        "This is an m3m provider health check. Return exactly a JSON object "
        '{"status":"ok"}. Do not inspect files, use tools, or perform any other action.'
    )

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()

    def run_prompt(self, prompt: str, provider: Optional[str] = None, output_schema: Optional[dict] = None) -> Tuple[bool, str]:
        """Run a configured provider, or fail closed when local-only is enabled."""
        prov = provider or self.config.active_provider
        if self.config.local_only and prov not in {"heuristic", "none"}:
            return False, "LLM provider disabled by local_only configuration"
        runners = {
            "codex": self._run_codex,
            "claude": self._run_claude,
            "opencode": self._run_opencode,
            "kimi": self._run_kimi,
            "api": self._run_api,
        }
        runner = runners.get(prov)
        if not runner:
            return False, f"Unknown LLM provider: {prov}"
        if prov == "codex":
            return self._run_codex(prompt, output_schema=output_schema)
        return runner(prompt)

    def verify_provider(
        self,
        provider: Optional[str] = None,
        live: bool = False,
    ) -> Tuple[bool, str]:
        """Verify provider configuration, optionally with a minimal live call.

        The live check uses a fixed prompt and the configured provider/model and
        timeout. It never receives a transcript and does not touch MemoryStore.
        """
        prov = provider or self.config.active_provider
        defaults = {
            "codex": "codex exec",
            "claude": "claude -p",
            "opencode": "opencode run",
            "kimi": "kimi",
            "api": "endpoint",
        }
        if prov not in defaults:
            return False, f"Provider '{scrub_secrets(str(prov))[:100]}' does not support verification"
        if self.config.local_only:
            return False, "Provider verification disabled by local_only configuration"

        if prov == "api":
            if not (self.config.api_base_url or "").strip():
                return False, "api provider requires api_base_url configuration"
            if not self.resolve_api_key():
                primary = scrub_secrets(str(self.config.api_key_env or "ZAI_API_KEY"))[:60]
                return False, f"api provider key not found (set {primary} or api_key in config)"
        else:
            _, error = self._configured_command(prov, defaults[prov])
            if error:
                return False, scrub_secrets(error)[:500]

        model = scrub_secrets(str(self.config.model_name or "default"))[:100]
        if not live:
            return True, f"Provider '{prov}' is configured (model={model})"

        schema = None
        if prov == "codex":
            schema = {
                "type": "object",
                "properties": {"status": {"type": "string", "enum": ["ok"]}},
                "required": ["status"],
                "additionalProperties": False,
            }
        success, output = self.run_prompt(
            self.HEALTH_CHECK_PROMPT,
            provider=prov,
            output_schema=schema,
        )
        if not success:
            detail = scrub_secrets(output or "provider returned no error detail")[:500]
            return False, f"Provider '{prov}' live check failed (model={model}): {detail}"
        if not output.strip():
            return False, f"Provider '{prov}' live check returned empty output (model={model})"
        return True, f"Provider '{prov}' live check passed (model={model}, timeout={self.config.provider_timeout_seconds}s)"

    def _configured_command(self, provider: str, default: str) -> Tuple[List[str], str]:
        command = getattr(self.config, f"{provider}_command", default)
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return [], f"Invalid {provider} command configuration: {exc}"
        if not parts:
            return [], f"Empty {provider} command configuration"
        if not shutil.which(parts[0]):
            return [], f"{provider.title()} CLI binary not found in PATH: {parts[0]}"
        return parts, ""

    @staticmethod
    def _schema() -> dict:
        return {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "scope": {"type": "string", "enum": ["global", "project"]},
                            "type": {"type": "string"},
                            "confidence": {"type": "number"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "entities": {"type": "array", "items": {"type": "string"}},
                            "contradicts_existing_id": {"type": ["string", "null"]},
                        },
                        "required": [
                            "content",
                            "scope",
                            "type",
                            "confidence",
                            "tags",
                            "entities",
                            "contradicts_existing_id",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["memories"],
            "additionalProperties": False,
        }

    def _provider_model(self, provider: str) -> str:
        """Resolve the configured model for a provider.

        ``model_name`` is the single place the user pins their choice. Codex
        aliases (luna/terra/mini) only mean something to codex and are never
        forwarded to other providers.
        """
        raw = (self.config.model_name or "").strip()
        if not raw or raw.lower() in {"default", "auto", "none"}:
            return ""
        if provider == "codex":
            return raw
        if raw.lower() in {"luna", "terra", "mini"}:
            return ""
        return raw

    def _run_codex(self, prompt: str, output_schema: Optional[dict] = None) -> Tuple[bool, str]:
        base_cmd, error = self._configured_command("codex", "codex exec")
        if error:
            return False, error
        raw_model = (self.config.model_name or "default").strip()
        codex_aliases = {
            "luna": "gpt-5.6-luna",
            "terra": "gpt-5.6-terra",
            "mini": "gpt-5.4-mini",
        }
        model = codex_aliases.get(raw_model.lower(), raw_model)
        effort = (self.config.reasoning_effort or "low").strip()
        cmd = base_cmd + [
            "--skip-git-repo-check",
            "--ephemeral",
            "-s", "read-only",
            "-c", "mcp_servers={}",
        ]
        if model and model.lower() not in {"default", "auto", "none", ""}:
            cmd.extend(["-c", f'model="{model}"'])
        if effort and effort.lower() not in {"default", "none", ""}:
            cmd.extend(["-c", f'reasoning_effort="{effort}"'])
        cmd.append(prompt)
        return self._run_simple(cmd)

    def _run_claude(self, prompt: str) -> Tuple[bool, str]:
        cmd, error = self._configured_command("claude", "claude -p")
        if error:
            return False, error
        model = self._provider_model("claude") or "haiku"
        cmd.extend(["--model", model, prompt])
        return self._run_simple(cmd)

    def _run_opencode(self, prompt: str) -> Tuple[bool, str]:
        cmd, error = self._configured_command("opencode", "opencode run")
        if error:
            return False, error
        model = self._provider_model("opencode")
        if model:
            cmd.extend(["--model", model])
        cmd.append(prompt)
        return self._run_simple(cmd)

    def _run_kimi(self, prompt: str) -> Tuple[bool, str]:
        cmd, error = self._configured_command("kimi", "kimi")
        if error:
            return False, error
        cmd.extend(["-p", prompt])
        return self._run_simple(cmd)

    def resolve_api_key(self) -> str:
        """Resolve the API key: explicit config value first, then env vars.

        The configured ``api_key_env`` name is checked first, followed by a
        small list of common aliases so a stock Z.ai/OpenAI setup works
        without extra configuration.
        """
        explicit = (getattr(self.config, "api_key", "") or "").strip()
        if explicit:
            return explicit
        env_names = []
        primary = (getattr(self.config, "api_key_env", "") or "").strip()
        if primary:
            env_names.append(primary)
        env_names.extend(["ZAI_API_KEY", "Z_AI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "OPENAI_API_KEY"])
        seen = set()
        for name in env_names:
            if name in seen:
                continue
            seen.add(name)
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        stripped = (text or "").strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if len(lines) < 2:
            return stripped
        if lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        return "\n".join(lines).strip()

    def _run_api(self, prompt: str) -> Tuple[bool, str]:
        """Call an OpenAI-compatible chat completions endpoint directly.

        Unlike the CLI providers this never spawns an agent session, so
        extraction runs invisibly in the background without polluting the
        user's chat history in any tool.
        """
        endpoint = (self.config.api_base_url or "").strip()
        if not endpoint:
            return False, "api provider requires api_base_url configuration"
        key = self.resolve_api_key()
        if not key:
            primary = (self.config.api_key_env or "ZAI_API_KEY").strip()
            return False, f"api provider key not found (set {primary} or api_key in config)"
        model = self._provider_model("api")
        if not model:
            return False, "api provider requires an explicit model_name in config"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        timeout = max(1, int(self.config.provider_timeout_seconds))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except OSError:
                detail = ""
            finally:
                exc.close()
            message = f"api provider HTTP error {exc.code}: {detail}".strip()
            return False, scrub_secrets(message)[:500]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            message = f"api provider request failed: {exc}"
            return False, scrub_secrets(message)[:500]
        try:
            parsed = json.loads(body)
        except ValueError:
            return False, "api provider returned a non-JSON response"
        if not isinstance(parsed, dict):
            return False, "api provider returned an unexpected JSON payload"
        error = parsed.get("error")
        if error:
            return False, scrub_secrets(f"api provider error: {error}")[:500]
        content = self._extract_api_content(parsed)
        if content is None:
            return False, "api provider returned no message content"
        return True, self._strip_code_fences(content)

    @staticmethod
    def _extract_api_content(parsed: dict) -> Optional[str]:
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = first.get("text")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            if parts:
                return "".join(parts)
        return None

    @staticmethod
    def _kill_process_group(process: "subprocess.Popen") -> None:
        """Kill the provider process and every descendant it spawned.

        Provider CLIs (codex, claude) boot their own helper processes — MCP
        servers, marketplace git checks, auth helpers. Killing only the direct
        child would orphan that whole tree, so on POSIX we signal the process
        group started with ``start_new_session=True``.
        """
        if os.name != "posix":
            process.kill()
            return
        try:
            group_id = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, OSError):
            group_id = None
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if group_id is not None:
                    os.killpg(group_id, sig)
                else:
                    process.send_signal(sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
            if group_id is None:
                return
            try:
                process.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                continue

    def _run_simple(self, cmd: List[str]) -> Tuple[bool, str]:
        timeout = max(1, int(self.config.provider_timeout_seconds))
        process = None
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=(os.name == "posix"),
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_process_group(process)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                return False, (
                    f"provider timed out after {timeout}s and its process tree "
                    "was terminated"
                )
            if process.returncode == 0:
                return True, (stdout or "").strip()
            return False, (stderr or "").strip() or (stdout or "").strip()
        except OSError as exc:
            return False, str(exc)
        finally:
            if process is not None and process.poll() is None:
                self._kill_process_group(process)
