"""Safety and compatibility tests for Claude and Codex hook installation."""

import json
import tempfile
import unittest
from pathlib import Path

from agent_m3m.hooks_installer import HooksInstaller


def _commands(entries):
    commands = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("command"), str):
            commands.append(entry["command"])
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                commands.append(hook["command"])
    return commands


class JsonHookInstallerTests(unittest.TestCase):
    def test_claude_migrates_only_m3m_entries_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            path = home / ".claude" / "settings.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "theme": "dark",
                "hooks": {
                    "SessionStart": [
                        {"type": "command", "command": "m3m context --legacy"},
                        {"type": "command", "command": '"/Applications/Agent Tools/bin/m3m" context --legacy'},
                        {"type": "command", "command": "keep --label 'm3m context'"},
                        {
                            "matcher": "startup",
                            "hooks": [
                                {"type": "command", "command": "m3m context"},
                                {"type": "command", "command": "keep-start"},
                            ],
                        },
                    ],
                    "SessionEnd": [{"type": "command", "command": "m3m save --stdin"}],
                    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-tool"}]}],
                },
            }), encoding="utf-8")
            installer = HooksInstaller(home_dir=home)

            self.assertTrue(installer.install_claude_hooks()[2])
            first = path.read_bytes()
            self.assertTrue(installer.install_claude_hooks()[2])
            self.assertEqual(path.read_bytes(), first)

            installed = json.loads(first)
            self.assertEqual(installed["theme"], "dark")
            self.assertEqual(_commands(installed["hooks"]["PreToolUse"]), ["keep-tool"])
            start = _commands(installed["hooks"]["SessionStart"])
            self.assertIn("keep --label 'm3m context'", start)
            self.assertIn("keep-start", start)
            self.assertEqual([value for value in start if value.startswith("m3m context")], ["m3m context"])
            self.assertEqual(
                _commands(installed["hooks"]["SessionEnd"]),
                ["m3m save --stdin --session-end --quiet"],
            )
            managed_start = [
                entry for entry in installed["hooks"]["SessionStart"]
                if isinstance(entry, dict)
                and _commands([entry]) == ["m3m context"]
            ]
            self.assertEqual(len(managed_start), 1)
            self.assertEqual(managed_start[0]["matcher"], "startup|resume|clear|compact")
            self.assertEqual(managed_start[0]["hooks"], [{
                "type": "command",
                "command": "m3m context",
                "timeout": 3,
            }])

    def test_quoted_absolute_m3m_path_is_managed_but_argument_mentions_are_not(self):
        entries = [
            {"type": "command", "command": '"/Applications/Agent Tools/bin/m3m" context --legacy'},
            {"type": "command", "command": "keep --executable '/Applications/Agent Tools/bin/m3m' context"},
        ]

        retained = HooksInstaller._remove_m3m_commands(entries, "context")

        self.assertEqual(retained, [entries[1]])

    def test_codex_preserves_mixed_hooks_and_stop_command_is_compatible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            path = home / ".codex" / "hooks.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "custom": {"enabled": True},
                "hooks": {
                    "SessionStart": [{"type": "command", "command": "m3m context --legacy"}],
                    "Stop": [{
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "m3m save --stdin"},
                            {"type": "command", "command": "keep-stop"},
                        ],
                    }],
                    "SessionEnd": [{"type": "command", "command": "keep-session-end"}],
                },
            }), encoding="utf-8")
            installer = HooksInstaller(home_dir=home)

            self.assertTrue(installer.install_codex_hooks()[2])
            first = path.read_bytes()
            self.assertTrue(installer.install_codex_hooks()[2])
            self.assertEqual(path.read_bytes(), first)

            installed = json.loads(first)
            self.assertEqual(installed["custom"], {"enabled": True})
            self.assertIn("keep-stop", _commands(installed["hooks"]["Stop"]))
            self.assertIn("keep-session-end", _commands(installed["hooks"]["SessionEnd"]))
            managed_stop = [
                value for value in _commands(installed["hooks"]["Stop"])
                if value.startswith("m3m save")
            ]
            self.assertEqual(managed_stop, ["m3m save --stdin --session-end --hook-output --quiet"])

    def test_malformed_existing_json_fails_without_changing_bytes(self):
        for agent, relative_path, method_name in (
            ("Claude", Path(".claude/settings.json"), "install_claude_hooks"),
            ("Codex", Path(".codex/hooks.json"), "install_codex_hooks"),
        ):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as temp_dir:
                home = Path(temp_dir)
                path = home / relative_path
                path.parent.mkdir(parents=True)
                original = b'{"hooks": [ invalid json\n'
                path.write_bytes(original)

                name, message, ok = getattr(HooksInstaller(home_dir=home), method_name)()

                self.assertFalse(ok, name)
                self.assertIn("left unchanged", message)
                self.assertEqual(path.read_bytes(), original)

    def test_repository_json_templates_use_nested_current_commands(self):
        root = Path(__file__).resolve().parent.parent
        claude = json.loads((root / "hooks" / "claude_hooks.json").read_text(encoding="utf-8"))
        codex = json.loads((root / "hooks" / "codex_hooks.json").read_text(encoding="utf-8"))

        self.assertTrue(all("hooks" in entry for entry in claude["hooks"]["SessionStart"]))
        self.assertTrue(all("hooks" in entry for entry in claude["hooks"]["SessionEnd"]))
        self.assertIn("m3m context", _commands(claude["hooks"]["SessionStart"]))
        self.assertIn("m3m save --stdin --session-end --quiet", _commands(claude["hooks"]["SessionEnd"]))
        self.assertIn(
            "m3m save --stdin --session-end --hook-output --quiet",
            _commands(codex["hooks"]["Stop"]),
        )


if __name__ == "__main__":
    unittest.main()
