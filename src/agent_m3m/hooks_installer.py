"""Safe lifecycle hook installation for supported coding agents."""

import json
import os
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
    def _remove_commands(entries: List[Any], marker: str) -> List[Any]:
        return [entry for entry in entries if marker not in json.dumps(entry, ensure_ascii=False)]

    @staticmethod
    def _entries(hooks: Dict[str, Any], event: str) -> List[Any]:
        value = hooks.get(event, [])
        return value if isinstance(value, list) else []

    @staticmethod
    def _remove_codex_m3m_save(entries: List[Any]) -> List[Any]:
        """Remove only m3m capture commands, retaining sibling Codex hooks."""
        cleaned: List[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                cleaned.append(entry)
                continue

            command = entry.get("command")
            if isinstance(command, str):
                if "m3m save" not in command:
                    cleaned.append(entry)
                continue

            nested_hooks = entry.get("hooks")
            if not isinstance(nested_hooks, list):
                cleaned.append(entry)
                continue

            retained = [
                hook
                for hook in nested_hooks
                if not (isinstance(hook, dict) and "m3m save" in str(hook.get("command", "")))
            ]
            if retained:
                cleaned.append({**entry, "hooks": retained})
        return cleaned

    def install_claude_hooks(self) -> Tuple[str, str, bool]:
        """Install SessionStart and SessionEnd without duplicating Claude hooks."""
        settings_file = self.home_dir / ".claude" / "settings.json"
        data: Dict[str, Any] = {}
        if settings_file.is_file():
            try:
                data = json.loads(settings_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("hooks"), dict):
            data["hooks"] = {}
        hooks = data.setdefault("hooks", {})
        hooks["SessionStart"] = self._remove_commands(self._entries(hooks, "SessionStart"), "m3m context")
        hooks["SessionEnd"] = self._remove_commands(self._entries(hooks, "SessionEnd"), "m3m save")
        hooks["Stop"] = self._remove_commands(self._entries(hooks, "Stop"), "m3m save")
        hooks["SessionStart"].append({"type": "command", "command": "m3m context", "timeout": 3})
        hooks["SessionEnd"].append({"type": "command", "command": "m3m save --stdin --session-end --quiet", "timeout": 3})
        try:
            self._atomic_json_write(settings_file, data)
            return ("Claude Code Hooks", f"Installed safely in {settings_file}", True)
        except (OSError, ValueError) as exc:
            return ("Claude Code Hooks", f"Failed to write settings: {exc}", False)

    def install_codex_hooks(self) -> Tuple[str, str, bool]:
        """Install Codex lifecycle hooks using valid JSON hook responses."""
        hooks_file = self.home_dir / ".codex" / "hooks.json"
        data: Dict[str, Any] = {}
        if hooks_file.is_file():
            try:
                data = json.loads(hooks_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("hooks"), dict):
            data["hooks"] = {}
        hooks = data.setdefault("hooks", {})
        hooks["SessionStart"] = self._remove_commands(self._entries(hooks, "SessionStart"), "m3m context")
        hooks["SessionEnd"] = self._remove_codex_m3m_save(self._entries(hooks, "SessionEnd"))
        hooks["Stop"] = self._remove_codex_m3m_save(self._entries(hooks, "Stop"))
        hooks["SessionStart"].append({
            "matcher": "startup|resume|clear|compact",
            "hooks": [{"type": "command", "command": "m3m context --hook-output", "timeout": 3}],
        })
        hooks["UserPromptSubmit"] = self._remove_commands(self._entries(hooks, "UserPromptSubmit"), "m3m context")
        hooks["UserPromptSubmit"].append({
            "matcher": "",
            "hooks": [{"type": "command", "command": "m3m context --hook-output --hook-event UserPromptSubmit", "timeout": 3}],
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
        lines = [line for line in content.splitlines() if "m3m " not in line]
        lines.extend([
            "",
            "[[hooks]]",
            'event = "SessionStart"',
            'command = "m3m context"',
            "",
            "[[hooks]]",
            'event = "SessionEnd"',
            'command = "m3m save --stdin --session-end --quiet"',
            "",
        ])
        try:
            self._atomic_text_write(path, "\n".join(lines))
            return ("Kimi Hooks", f"Installed in {path}", True)
        except OSError as exc:
            return ("Kimi Hooks", f"Failed to write hooks: {exc}", False)

    def install_opencode_hooks(self) -> Tuple[str, str, bool]:
        path = self.home_dir / ".config" / "opencode" / "plugins" / "m3m-memory.js"
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
        context = (await runM3m(prompt ? ["context", "--prompt", prompt] : ["context"])).trim();
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
