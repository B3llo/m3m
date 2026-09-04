"""Regression tests for configuration persistence and hook installation."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.hooks_installer import HooksInstaller


class ConfigPersistenceTests(unittest.TestCase):
    def test_config_round_trips_shell_commands_losslessly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            global_path = Path(temp_dir) / "global"
            project_path = Path(temp_dir) / "project"
            config = Config(global_path=global_path, project_path=project_path)
            config.codex_command = 'tool --arg "hello world" --path "C:\\work\\project"\nnext-line'
            config.claude_command = "claude -p 'quoted value' --label café"
            config.save_config()

            loaded = Config(global_path=global_path, project_path=project_path)

            self.assertEqual(loaded.codex_command, config.codex_command)
            self.assertEqual(loaded.claude_command, config.claude_command)

            with mock.patch("agent_m3m.config.tomllib", None):
                fallback_loaded = Config(global_path=global_path, project_path=project_path)
            self.assertEqual(fallback_loaded.codex_command, config.codex_command)
            self.assertEqual(fallback_loaded.claude_command, config.claude_command)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable to Windows")
    def test_config_save_enforces_owner_only_permissions_on_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            global_path = Path(temp_dir) / "global"
            global_path.mkdir()
            config_file = global_path / "config.toml"
            config_file.write_text("[general]\n", encoding="utf-8")
            config_file.chmod(0o644)

            Config(global_path=global_path, project_path=Path(temp_dir) / "project").save_config()

            self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)


class KimiInstallerTests(unittest.TestCase):
    def test_kimi_install_replaces_complete_managed_blocks_and_preserves_unrelated_toml(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            hooks_file = home / ".kimi" / "hooks.toml"
            hooks_file.parent.mkdir(parents=True)
            hooks_file.write_text(
                "# existing Kimi configuration\n"
                "[[hooks]]\n"
                'event = "BeforeTool"\n'
                'command = "keep-hook --mentions m3m context in an argument"\n'
                'timeout = 11\n\n'
                "[[hooks]]\n"
                'event = "SessionStart"\n'
                'command = "m3m context --legacy"\n'
                'timeout = 2\n\n'
                "[hooks.environment]\n"
                'SOURCE = "m3m"\n\n'
                "[[hooks]]\n"
                'event = "AfterTool"\n'
                'command = "keep-after-hook"\n\n'
                "[[hooks]]\n"
                'event = "SessionEnd"\n'
                "command = 'm3m save --stdin'\n"
                'timeout = 2\n\n'
                "[ui]\n"
                'theme = "dark"\n',
                encoding="utf-8",
            )
            installer = HooksInstaller(home_dir=home)

            self.assertTrue(installer.install_kimi_hooks()[2])
            first = hooks_file.read_text(encoding="utf-8")
            self.assertTrue(installer.install_kimi_hooks()[2])
            second = hooks_file.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertIn('command = "keep-hook --mentions m3m context in an argument"', second)
            self.assertIn('command = "keep-after-hook"', second)
            self.assertIn('[ui]\ntheme = "dark"', second)
            self.assertNotIn("m3m context --legacy", second)
            self.assertNotIn("command = 'm3m save --stdin'", second)
            self.assertEqual(second.count("[[hooks]]"), 4)
            self.assertEqual(second.count('command = "m3m context"'), 1)
            self.assertEqual(second.count('command = "m3m save --stdin --session-end --quiet"'), 1)


if __name__ == "__main__":
    unittest.main()
