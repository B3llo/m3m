"""Safe lifecycle hook installation for supported coding agents."""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class HooksInstaller:
    SUPPORTED_AGENTS = ("claude", "codex", "kimi", "opencode")

    def __init__(self, home_dir: Optional[Path] = None):
        self.home_dir = (home_dir or Path.home()).resolve()

    def install_all(self, agents: Optional[List[str]] = None) -> List[Tuple[str, str, bool]]:
        selected = agents or list(self.SUPPORTED_AGENTS)
        results = []
        for agent in selected:
            installer = getattr(self, f"install_{agent}_hooks", None)
            if installer:
                results.append(installer())
            else:
                results.append((agent, f"Unsupported agent: {agent}", False))
        return results

    def uninstall_all(self, agents: List[str]) -> List[Tuple[str, str, bool]]:
        """Remove only m3m-managed integration entries for selected agents."""
        results = []
        for agent in agents:
            uninstaller = getattr(self, f"uninstall_{agent}_hooks", None)
            if uninstaller:
                results.append(uninstaller())
            else:
                results.append((agent, f"Unsupported agent: {agent}", False))
        return results

    def _atomic_json_write(self, path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            backup = path.with_name(path.name + ".bak")
            if not backup.exists():
                shutil.copy2(path, backup)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
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
                temp_path.unlink()

    def _atomic_text_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            backup = path.with_name(path.name + ".bak")
            if not backup.exists():
                shutil.copy2(path, backup)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _entries(hooks: Dict[str, Any], event: str) -> List[Any]:
        value = hooks.get(event, [])
        return value if isinstance(value, list) else []

    @staticmethod
    def _is_m3m_command(command: Any, subcommand: str) -> bool:
        if not isinstance(command, str):
            return False
        return bool(re.match(
            rf"^(?:"
            rf'"/[^"\r\n]*/m3m"'
            rf"|'/[^'\r\n]*/m3m'"
            rf"|(?:\S*[/\\])?m3m"
            rf")\s+{re.escape(subcommand)}(?:\s|$)",
            command.strip(),
        ))

    @classmethod
    def _remove_m3m_commands(cls, entries: List[Any], subcommand: str) -> List[Any]:
        """Remove only managed commands, retaining legacy entries and siblings."""
        cleaned: List[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                cleaned.append(entry)
                continue

            command = entry.get("command")
            if isinstance(command, str):
                if not cls._is_m3m_command(command, subcommand):
                    cleaned.append(entry)
                continue

            nested_hooks = entry.get("hooks")
            if not isinstance(nested_hooks, list):
                cleaned.append(entry)
                continue

            retained = [
                hook
                for hook in nested_hooks
                if not (
                    isinstance(hook, dict)
                    and cls._is_m3m_command(hook.get("command"), subcommand)
                )
            ]
            if retained:
                cleaned.append({**entry, "hooks": retained})
        return cleaned

    @staticmethod
    def _load_json_config(path: Path, label: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Read an existing JSON config without ever repairing it destructively."""
        if not path.is_file():
            return {}, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            return None, f"Invalid {label} JSON in {path}; file left unchanged: {exc}"
        if not isinstance(data, dict):
            return None, f"Invalid {label} JSON in {path}; root must be an object and file was left unchanged"
        hooks = data.get("hooks")
        if "hooks" in data and not isinstance(hooks, dict):
            return None, f"Invalid {label} hooks in {path}; hooks must be an object and file was left unchanged"
        if isinstance(hooks, dict):
            invalid_events = [event for event, entries in hooks.items() if not isinstance(entries, list)]
            if invalid_events:
                event = str(invalid_events[0])[:100]
                return None, (
                    f"Invalid {label} hooks in {path}; event {event!r} must be a list "
                    "and file was left unchanged"
                )
        return data, None

    @classmethod
    def _remove_kimi_m3m_hook_blocks(cls, content: str) -> str:
        """Remove complete m3m-owned ``[[hooks]]`` entries from Kimi TOML.

        Removing only the command line leaves the preceding array-table header
        behind and can change how later keys are parsed.  Work block-by-block
        instead, while preserving unrelated hook entries and other tables.
        """
        lines = content.splitlines(keepends=True)
        hook_header = re.compile(r"^\s*\[\[\s*hooks\s*\]\]\s*(?:#.*)?$")
        any_table = re.compile(r"^\s*\[{1,2}\s*[^\]]+?\s*\]{1,2}\s*(?:#.*)?$")
        hook_child_table = re.compile(r"^\s*\[{1,2}\s*hooks\s*\.")
        command = re.compile(
            r"^\s*command\s*=\s*(?P<value>\"(?:[^\"\\]|\\.)*\"|'[^']*')\s*(?:#.*)?$",
            re.MULTILINE,
        )

        output: List[str] = []
        index = 0
        while index < len(lines):
            if not hook_header.match(lines[index].rstrip("\r\n")):
                output.append(lines[index])
                index += 1
                continue

            end = index + 1
            while end < len(lines):
                candidate = lines[end].rstrip("\r\n")
                if hook_header.match(candidate):
                    break
                if any_table.match(candidate) and not hook_child_table.match(candidate):
                    break
                end += 1
            block = "".join(lines[index:end])
            managed = False
            for match in command.finditer(block):
                assignment = match.group("value")
                try:
                    value = json.loads(assignment) if assignment.startswith('"') else assignment[1:-1]
                except (TypeError, ValueError):
                    value = assignment
                if isinstance(value, str) and (
                    cls._is_m3m_command(value, "context")
                    or cls._is_m3m_command(value, "save")
                ):
                    managed = True
                    break
            if not managed:
                output.extend(lines[index:end])
            index = end

        return "".join(output).rstrip()

    def install_claude_hooks(self) -> Tuple[str, str, bool]:
        """Install SessionStart and SessionEnd without duplicating Claude hooks."""
        settings_file = self.home_dir / ".claude" / "settings.json"
        data, error = self._load_json_config(settings_file, "Claude settings")
        if error or data is None:
            return ("Claude Code Hooks", error or "Failed to read Claude settings", False)
        hooks = data.setdefault("hooks", {})
        hooks["SessionStart"] = self._remove_m3m_commands(self._entries(hooks, "SessionStart"), "context")
        hooks["SessionEnd"] = self._remove_m3m_commands(self._entries(hooks, "SessionEnd"), "save")
        hooks["Stop"] = self._remove_m3m_commands(self._entries(hooks, "Stop"), "save")
        hooks["SessionStart"].append({
            "matcher": "startup|resume|clear|compact",
            "hooks": [{"type": "command", "command": "m3m context", "timeout": 3}],
        })
        hooks["SessionEnd"].append({
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": "m3m save --stdin --session-end --quiet",
                "timeout": 3,
            }],
        })
        try:
            self._atomic_json_write(settings_file, data)
            return ("Claude Code Hooks", f"Installed safely in {settings_file}", True)
        except (OSError, ValueError) as exc:
            return ("Claude Code Hooks", f"Failed to write settings: {exc}", False)

    def install_codex_hooks(self) -> Tuple[str, str, bool]:
        """Install Codex lifecycle hooks using valid JSON hook responses."""
        hooks_file = self.home_dir / ".codex" / "hooks.json"
        data, error = self._load_json_config(hooks_file, "Codex hooks")
        if error or data is None:
            return ("OpenAI Codex Hooks", error or "Failed to read Codex hooks", False)
        hooks = data.setdefault("hooks", {})
        hooks["SessionStart"] = self._remove_m3m_commands(self._entries(hooks, "SessionStart"), "context")
        hooks["SessionEnd"] = self._remove_m3m_commands(self._entries(hooks, "SessionEnd"), "save")
        hooks["Stop"] = self._remove_m3m_commands(self._entries(hooks, "Stop"), "save")
        hooks["SessionStart"].append({
            "matcher": "startup|resume|clear|compact",
            "hooks": [{"type": "command", "command": "m3m context --hook-output", "timeout": 3}],
        })
        hooks["UserPromptSubmit"] = self._remove_m3m_commands(
            self._entries(hooks, "UserPromptSubmit"),
            "context",
        )
        hooks["UserPromptSubmit"].append({
            "matcher": "",
            "hooks": [{"type": "command", "command": "m3m context --hook-output --hook-event UserPromptSubmit --no-record-usage", "timeout": 3}],
        })
        hooks["Stop"].append({
            "matcher": "",
            "hooks": [{"type": "command", "command": "m3m save --stdin --session-end --hook-output --quiet", "timeout": 3}],
        })
        try:
            self._atomic_json_write(hooks_file, data)
            return ("OpenAI Codex Hooks", f"Installed safely in {hooks_file}", True)
        except (OSError, ValueError) as exc:
            return ("OpenAI Codex Hooks", f"Failed to write hooks: {exc}", False)

    def install_kimi_hooks(self) -> Tuple[str, str, bool]:
        path = self.home_dir / ".kimi" / "hooks.toml"
        content = path.read_text(encoding="utf-8") if path.is_file() else "# Kimi CLI hooks\n"
        retained = self._remove_kimi_m3m_hook_blocks(content)
        managed_hooks = (
            '[[hooks]]\n'
            'event = "SessionStart"\n'
            'command = "m3m context"\n\n'
            '[[hooks]]\n'
            'event = "SessionEnd"\n'
            'command = "m3m save --stdin --session-end --quiet"\n'
        )
        updated = f"{retained}\n\n{managed_hooks}" if retained else managed_hooks
        try:
            self._atomic_text_write(path, updated)
            return ("Kimi Hooks", f"Installed in {path}", True)
        except OSError as exc:
            return ("Kimi Hooks", f"Failed to write hooks: {exc}", False)

    def install_opencode_hooks(self) -> Tuple[str, str, bool]:
        path = self.home_dir / ".config" / "opencode" / "plugins" / "m3m-memory.js"
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                return ("OpenCode Plugin", f"Failed to read existing plugin; file left unchanged: {exc}", False)
            if not existing.startswith("// OpenCode memory harness for m3m.\n"):
                return ("OpenCode Plugin", f"Refusing to overwrite unrecognized plugin at {path}", False)
        plugin = """// OpenCode memory harness for m3m.
// OpenCode loads local plugins as ESM modules from ~/.config/opencode/plugins.
//
// Injection happens pre-turn, matching the push-core/pull-tail pattern used by
// Zep, Letta and Claude Code: memory must exist in the system prompt before
// the model's first generated token of a turn.
// - chat.message stashes the latest user text and invalidates the cached
//   system context once per user turn.
// - experimental.chat.system.transform injects the m3m context block into the
//   system prompt on every LLM request, rebuilt at most once per turn (with
//   prompt-relevant archival retrieval when a user text is stashed).
// - session.idle captures the transcript after the session goes idle.
import { spawn } from "node:child_process";

const M3M_CONTEXT_MARKER = "<m3m-persistent-memory>";
const M3M_TIMEOUT_MS = 10000;
const lastSavedTranscript = new Map();
const sessionContext = new Map();
const sessionPrompt = new Map();

function killTree(child) {
  // m3m may have spawned its own provider CLI; signal the whole group so no
  // descendant survives as an orphan.
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {
    try { child.kill("SIGTERM"); } catch {}
  }
  setTimeout(() => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    try { process.kill(-child.pid, "SIGKILL"); } catch {
      try { child.kill("SIGKILL"); } catch {}
    }
  }, 2000).unref();
}

function runM3m(args, input, timeoutMs = M3M_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const child = spawn("m3m", args, { stdio: ["pipe", "pipe", "pipe"], detached: true });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      killTree(child);
      reject(new Error(`m3m ${args[0]} timed out after ${timeoutMs}ms and its process tree was terminated`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code === 0) resolve(stdout);
      else reject(new Error(stderr.trim() || `m3m exited with code ${code}`));
    });
    child.stdin.end(input);
  });
}

function transcriptFromMessages(response) {
  const messages = Array.isArray(response) ? response : response?.data;
  if (!Array.isArray(messages)) return "";

  return messages.flatMap(({ info, parts = [] }) => {
    if (info?.role !== "user" && info?.role !== "assistant") return [];
    const text = parts
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\\n")
      .trim();
    if (!text || text.startsWith(M3M_CONTEXT_MARKER)) return [];
    return [`${info.role.toUpperCase()}: ${text}`];
  }).join("\\n\\n");
}

function textFromParts(parts) {
  return (Array.isArray(parts) ? parts : [])
    .filter((part) => part?.type === "text" && typeof part.text === "string" && !part.text.startsWith(M3M_CONTEXT_MARKER))
    .map((part) => part.text)
    .join("\\n")
    .trim();
}

export const M3mMemoryPlugin = async ({ client }) => ({
  "chat.message": async (input, output) => {
    // New user turn: stash the text for relevance retrieval and force the
    // system context to rebuild on the next LLM request of this turn.
    const sessionID = input?.sessionID;
    if (!sessionID) return;
    const text = textFromParts(output?.parts);
    if (text) sessionPrompt.set(sessionID, text);
    sessionContext.delete(sessionID);
  },

  "experimental.chat.system.transform": async (input, output) => {
    const sessionID = input?.sessionID;
    if (!sessionID || !Array.isArray(output?.system)) return;
    let context = sessionContext.get(sessionID);
    if (context === undefined) {
      const prompt = sessionPrompt.get(sessionID);
      try {
        // Per-turn injection runs on every LLM request: skip usage writes to
        // avoid rewriting memory files under hook bursts. SessionStart hooks
        // and manual `m3m context` keep the reinforcement signal.
        context = (await runM3m(prompt ? ["context", "--prompt", prompt, "--no-record-usage"] : ["context", "--no-record-usage"])).trim();
        sessionContext.set(sessionID, context);
      } catch (error) {
        await client.app.log({ body: { service: "m3m", level: "warn", message: `context injection failed: ${error.message}` } });
        return;
      }
    }
    if (!context) return;
    output.system.push(`${M3M_CONTEXT_MARKER}\\n${context}`);
  },

  event: async ({ event }) => {
    if (event.type === "session.idle") {
      const sessionID = event.properties.sessionID;
      try {
        const transcript = transcriptFromMessages(await client.session.messages({ path: { id: sessionID } }));
        if (!transcript || lastSavedTranscript.get(sessionID) === transcript) return;
        await runM3m(["save", "--stdin", "--session-end", "--quiet"], transcript);
        lastSavedTranscript.set(sessionID, transcript);
      } catch (error) {
        await client.app.log({ body: { service: "m3m", level: "warn", message: `memory capture failed: ${error.message}` } });
      }
      return;
    }

    if (event.type === "session.deleted") {
      const sessionID = event.properties.info.id;
      lastSavedTranscript.delete(sessionID);
      sessionContext.delete(sessionID);
      sessionPrompt.delete(sessionID);
    }
  },
});
"""
        try:
            self._atomic_text_write(path, plugin)
            return ("OpenCode Plugin", f"Installed in {path}", True)
        except OSError as exc:
            return ("OpenCode Plugin", f"Failed to write plugin: {exc}", False)

    def _uninstall_json_hooks(self, path: Path, label: str) -> Tuple[str, str, bool]:
        if not path.exists():
            return (label, f"No configuration found at {path}; nothing to remove", True)
        data, error = self._load_json_config(path, label)
        if error or data is None:
            return (label, error or f"Failed to read {path}", False)

        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return (label, f"No m3m hooks found in {path}", True)

        changed = False
        for event, subcommands in {
            "SessionStart": ("context",),
            "UserPromptSubmit": ("context",),
            "SessionEnd": ("save",),
            "Stop": ("save",),
        }.items():
            original = self._entries(hooks, event)
            cleaned = original
            for subcommand in subcommands:
                cleaned = self._remove_m3m_commands(cleaned, subcommand)
            if cleaned != original:
                hooks[event] = cleaned
                changed = True

        if not changed:
            return (label, f"No m3m hooks found in {path}", True)
        try:
            self._atomic_json_write(path, data)
            return (label, f"Removed m3m hooks from {path}", True)
        except (OSError, ValueError) as exc:
            return (label, f"Failed to write settings: {exc}", False)

    def uninstall_claude_hooks(self) -> Tuple[str, str, bool]:
        return self._uninstall_json_hooks(
            self.home_dir / ".claude" / "settings.json",
            "Claude Code Hooks",
        )

    def uninstall_codex_hooks(self) -> Tuple[str, str, bool]:
        return self._uninstall_json_hooks(
            self.home_dir / ".codex" / "hooks.json",
            "OpenAI Codex Hooks",
        )

    def uninstall_kimi_hooks(self) -> Tuple[str, str, bool]:
        path = self.home_dir / ".kimi" / "hooks.toml"
        if not path.exists():
            return ("Kimi Hooks", f"No configuration found at {path}; nothing to remove", True)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return ("Kimi Hooks", f"Failed to read hooks; file left unchanged: {exc}", False)
        retained = self._remove_kimi_m3m_hook_blocks(content)
        if retained.rstrip() == content.rstrip():
            return ("Kimi Hooks", f"No m3m hooks found in {path}", True)
        updated = f"{retained}\n" if retained else ""
        try:
            self._atomic_text_write(path, updated)
            return ("Kimi Hooks", f"Removed m3m hooks from {path}", True)
        except OSError as exc:
            return ("Kimi Hooks", f"Failed to write hooks: {exc}", False)

    def uninstall_opencode_hooks(self) -> Tuple[str, str, bool]:
        path = self.home_dir / ".config" / "opencode" / "plugins" / "m3m-memory.js"
        if not path.exists():
            return ("OpenCode Plugin", f"No plugin found at {path}; nothing to remove", True)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return ("OpenCode Plugin", f"Failed to read plugin; file left unchanged: {exc}", False)
        if not (
            content.startswith("// OpenCode memory harness for m3m.\n")
            and "export const M3mMemoryPlugin" in content
        ):
            return ("OpenCode Plugin", f"Refusing to remove unrecognized plugin at {path}", False)
        try:
            path.unlink()
            return ("OpenCode Plugin", f"Removed m3m plugin from {path}", True)
        except OSError as exc:
            return ("OpenCode Plugin", f"Failed to remove plugin: {exc}", False)
