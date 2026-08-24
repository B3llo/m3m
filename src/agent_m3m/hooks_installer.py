"""HooksInstaller: Automatically wires m3m hooks into Claude Code, Codex CLI, and Kimi CLI configuration files."""

import json
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional


class HooksInstaller:
    def __init__(self, home_dir: Optional[Path] = None):
        self.home_dir = home_dir or Path.home()

    def install_all(self) -> List[Tuple[str, str, bool]]:
        """Install m3m hooks across supported agent configuration files."""
        results = []
        results.append(self.install_claude_hooks())
        results.append(self.install_codex_hooks())
        return results

    def install_claude_hooks(self) -> Tuple[str, str, bool]:
        """Wire hooks into ~/.claude/settings.json."""
        claude_dir = self.home_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file = claude_dir / "settings.json"

        data: Dict[str, Any] = {}
        if settings_file.is_file():
            try:
                data = json.loads(settings_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        if "hooks" not in data:
            data["hooks"] = {}

        hooks = data["hooks"]

        # Ensure SessionStart hook
        if "SessionStart" not in hooks:
            hooks["SessionStart"] = []
        if not any("m3m context" in str(h) for h in hooks["SessionStart"]):
            hooks["SessionStart"].append({"type": "command", "command": "m3m context"})

        # Ensure Stop hook
        if "Stop" not in hooks:
            hooks["Stop"] = []
        if not any("m3m save" in str(h) for h in hooks["Stop"]):
            hooks["Stop"].append({"type": "command", "command": "m3m save --stdin"})

        try:
            settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return ("Claude Code Hooks", f"Wired automatically in {settings_file}", True)
        except Exception as e:
            return ("Claude Code Hooks", f"Failed to write settings: {e}", False)

    def install_codex_hooks(self) -> Tuple[str, str, bool]:
        """Wire hooks into ~/.codex/hooks.json and enable them in config.toml."""
        codex_dir = self.home_dir / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        hooks_file = codex_dir / "hooks.json"
        config_file = codex_dir / "config.toml"

        data: Dict[str, Any] = {}
        if hooks_file.is_file():
            try:
                data = json.loads(hooks_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        if "hooks" not in data:
            data["hooks"] = {}

        hooks = data["hooks"]

        # Ensure SessionStart hook
        if "SessionStart" not in hooks:
            hooks["SessionStart"] = []
        if not any("m3m context" in str(h) for h in hooks["SessionStart"]):
            hooks["SessionStart"].append({"type": "command", "command": "m3m context"})

        # Ensure Stop hook
        if "Stop" not in hooks:
            hooks["Stop"] = []
        if not any("m3m save" in str(h) for h in hooks["Stop"]):
            hooks["Stop"].append({"type": "command", "command": "m3m save --stdin"})

        try:
            hooks_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

            # Also ensure hooks.state is enabled in config.toml if it exists
            if config_file.is_file():
                config_content = config_file.read_text(encoding="utf-8")
                if "[hooks.state]" in config_content:
                    # Replace enabled = false under m3m hooks with enabled = true
                    lines = config_content.splitlines()
                    new_lines = []
                    in_m3m_state = False
                    for line in lines:
                        if "hooks.json:stop" in line or "hooks.json:session_start" in line:
                            in_m3m_state = True
                        elif line.startswith("[") and not ("stop" in line or "session_start" in line):
                            in_m3m_state = False

                        if in_m3m_state and "enabled = false" in line:
                            line = "enabled = true"
                        new_lines.append(line)
                    config_file.write_text("\n".join(new_lines), encoding="utf-8")

            return ("OpenAI Codex Hooks", f"Wired & enabled in {hooks_file}", True)
        except Exception as e:
            return ("OpenAI Codex Hooks", f"Failed to write hooks: {e}", False)
