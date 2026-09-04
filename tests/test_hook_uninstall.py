"""Selective hook uninstall behavior and CLI coverage."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_m3m import cli
from agent_m3m.hooks_installer import HooksInstaller


class HookUninstallTests(unittest.TestCase):
    def test_json_uninstall_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            installer = HooksInstaller(home_dir=home)
            for install, uninstall, relative in (
                (installer.install_claude_hooks, installer.uninstall_claude_hooks, ".claude/settings.json"),
                (installer.install_codex_hooks, installer.uninstall_codex_hooks, ".codex/hooks.json"),
            ):
                path = home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({
                    "theme": "dark",
                    "hooks": {"Stop": [{"matcher": "", "hooks": [
                        {"type": "command", "command": "custom-tool save"},
                    ]}]},
                }), encoding="utf-8")
                self.assertTrue(install()[2])
                self.assertTrue(uninstall()[2])
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["theme"], "dark")
                serialized = json.dumps(data)
                self.assertIn("custom-tool save", serialized)
                self.assertNotIn("m3m context", serialized)
                self.assertNotIn("m3m save", serialized)

    def test_malformed_json_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            path = home / ".codex" / "hooks.json"
            path.parent.mkdir(parents=True)
            original = b'{"hooks": '
            path.write_bytes(original)

            _name, _message, ok = HooksInstaller(home_dir=home).uninstall_codex_hooks()

            self.assertFalse(ok)
            self.assertEqual(path.read_bytes(), original)

    def test_kimi_uninstall_removes_only_managed_blocks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            installer = HooksInstaller(home_dir=home)
            path = home / ".kimi" / "hooks.toml"
            path.parent.mkdir(parents=True)
            path.write_text(
                'title = "keep"\n\n[[hooks]]\nevent = "Stop"\ncommand = "custom-tool"\n',
                encoding="utf-8",
            )
            self.assertTrue(installer.install_kimi_hooks()[2])
            self.assertTrue(installer.uninstall_kimi_hooks()[2])
            content = path.read_text(encoding="utf-8")
            self.assertIn('command = "custom-tool"', content)
            self.assertNotIn('command = "m3m ', content)

    def test_opencode_refuses_unknown_file_and_removes_managed_plugin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            installer = HooksInstaller(home_dir=home)
            path = home / ".config" / "opencode" / "plugins" / "m3m-memory.js"
            path.parent.mkdir(parents=True)
            path.write_text("// user file\n", encoding="utf-8")
            self.assertFalse(installer.uninstall_opencode_hooks()[2])
            self.assertTrue(path.exists())
            self.assertFalse(installer.install_opencode_hooks()[2])
            self.assertEqual(path.read_text(encoding="utf-8"), "// user file\n")

            path.unlink()
            self.assertTrue(installer.install_opencode_hooks()[2])
            self.assertTrue(installer.uninstall_opencode_hooks()[2])
            self.assertFalse(path.exists())

    def test_cli_uninstall_uses_selected_agents(self):
        results = [("Codex", "removed", True)]
        with mock.patch.object(cli.sys, "argv", ["m3m", "hooks", "uninstall", "--agents", "codex"]), \
                mock.patch.object(cli.HooksInstaller, "uninstall_all", return_value=results) as uninstall:
            cli.main()
        uninstall.assert_called_once_with(["codex"])


if __name__ == "__main__":
    unittest.main()
