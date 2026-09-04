"""Focused tests for the explicit macOS queue LaunchAgent manager."""

from pathlib import Path
import plistlib
import io
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_m3m.config import Config
from agent_m3m.macos_scheduler import MacOSQueueScheduler, SchedulerError


class TestMacOSQueueScheduler(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = Config(global_path=root / "global", project_path=root / "project" / ".agent-m3m")
        self.commands = []

        def runner(command, **kwargs):
            self.commands.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        self.scheduler = MacOSQueueScheduler(
            config=self.config,
            home_dir=root / "home",
            executable="/opt/homebrew/bin/m3m",
            runner=runner,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_install_writes_per_project_plist_and_loads_only_its_label(self):
        status = self.scheduler.install(interval_seconds=120)

        self.assertTrue(status.installed)
        self.assertTrue(status.loaded)
        self.assertEqual(status.interval_seconds, 120)
        with status.plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        self.assertEqual(payload["Label"], self.scheduler.label)
        self.assertEqual(payload["ProgramArguments"], ["/opt/homebrew/bin/m3m", "queue", "process", "--limit", "10"])
        self.assertEqual(payload["WorkingDirectory"], str(self.config.project_root))
        self.assertEqual(payload["StartInterval"], 120)
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(self.commands[0][0][0:2], ["launchctl", "bootout"])
        self.assertEqual(self.commands[1][0][0:2], ["launchctl", "bootstrap"])
        self.assertEqual(self.commands[1][0][-1], str(status.plist_path))
        self.assertEqual(self.commands[2][0][0:2], ["launchctl", "print"])

    def test_status_is_read_only_when_not_installed(self):
        status = self.scheduler.status()

        self.assertFalse(status.installed)
        self.assertFalse(status.plist_path.parent.exists())
        self.assertEqual(self.commands, [])

    def test_status_reports_valid_plist_that_is_not_loaded(self):
        self.scheduler.plist_path.parent.mkdir(parents=True)
        with self.scheduler.plist_path.open("wb") as handle:
            plistlib.dump(self.scheduler._plist_payload(300, "/opt/homebrew/bin/m3m"), handle)

        def not_loaded_runner(command, **kwargs):
            self.commands.append((command, kwargs))
            return subprocess.CompletedProcess(command, 113, "", "Could not find service")

        self.scheduler.runner = not_loaded_runner
        status = self.scheduler.status()

        self.assertTrue(status.installed)
        self.assertFalse(status.loaded)
        self.assertEqual(self.commands[0][0][0:2], ["launchctl", "print"])

    def test_install_removes_only_its_new_plist_when_bootstrap_fails(self):
        def bootstrap_failure(command, **kwargs):
            self.commands.append((command, kwargs))
            if command[1] == "bootstrap":
                raise subprocess.CalledProcessError(5, command, stderr="bootstrap failed")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.scheduler.runner = bootstrap_failure

        with self.assertRaisesRegex(SchedulerError, "plist removed"):
            self.scheduler.install()
        self.assertFalse(self.scheduler.plist_path.exists())
        self.assertEqual(self.commands[1][0][0:2], ["launchctl", "bootstrap"])

    def test_bootstrap_failure_does_not_remove_a_plist_changed_after_write(self):
        replacement = b"replacement by another process"

        def bootstrap_failure(command, **kwargs):
            self.commands.append((command, kwargs))
            if command[1] == "bootstrap":
                self.scheduler.plist_path.write_bytes(replacement)
                raise subprocess.CalledProcessError(5, command, stderr="bootstrap failed")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.scheduler.runner = bootstrap_failure

        with self.assertRaisesRegex(SchedulerError, "left untouched"):
            self.scheduler.install()
        self.assertEqual(self.scheduler.plist_path.read_bytes(), replacement)

    def test_install_is_idempotent_for_the_same_plist(self):
        first = self.scheduler.install()
        first_contents = first.plist_path.read_bytes()
        second = self.scheduler.install()

        self.assertEqual(first.plist_path, second.plist_path)
        self.assertEqual(first_contents, second.plist_path.read_bytes())
        self.assertEqual(len(self.commands), 6)

    def test_uninstall_removes_only_the_identified_plist(self):
        self.scheduler.install()
        sibling = self.scheduler.plist_path.parent / "com.example.keep.plist"
        sibling.write_text("keep", encoding="utf-8")

        self.assertTrue(self.scheduler.uninstall())
        self.assertFalse(self.scheduler.plist_path.exists())
        self.assertTrue(sibling.exists())
        self.assertFalse(self.scheduler.uninstall())

    def test_uninstall_refuses_a_plist_with_an_unexpected_label(self):
        self.scheduler.plist_path.parent.mkdir(parents=True)
        with self.scheduler.plist_path.open("wb") as handle:
            plistlib.dump({"Label": "com.example.not-agent-m3m"}, handle)

        with self.assertRaises(SchedulerError):
            self.scheduler.uninstall()
        self.assertTrue(self.scheduler.plist_path.exists())

    def test_rejects_too_frequent_interval_and_missing_executable(self):
        with self.assertRaises(SchedulerError):
            self.scheduler.install(interval_seconds=29)
        missing = MacOSQueueScheduler(config=self.config, home_dir=Path(self.temp_dir.name) / "other-home")
        with mock.patch("agent_m3m.macos_scheduler.shutil.which", return_value=None):
            with self.assertRaises(SchedulerError):
                missing.install()

    def test_cli_scheduler_commands_delegate_without_installing_in_the_test(self):
        from agent_m3m import cli

        installed = mock.Mock()
        installed.plist_path = Path("/tmp/agent-m3m.plist")
        installed.interval_seconds = 300
        absent = mock.Mock(installed=False, plist_path=Path("/tmp/agent-m3m.plist"))
        scheduler = mock.Mock()
        scheduler.install.return_value = installed
        scheduler.status.return_value = absent
        scheduler.uninstall.return_value = True

        with mock.patch.object(cli, "Config", return_value=self.config), \
             mock.patch.object(cli, "MacOSQueueScheduler", return_value=scheduler), \
             mock.patch.object(sys, "argv", ["m3m", "queue", "install-scheduler", "--interval", "120"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            cli.main()
        scheduler.install.assert_called_once_with(120)

        scheduler.reset_mock()
        scheduler.status.return_value = absent
        with mock.patch.object(cli, "Config", return_value=self.config), \
             mock.patch.object(cli, "MacOSQueueScheduler", return_value=scheduler), \
             mock.patch.object(sys, "argv", ["m3m", "queue", "scheduler-status"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            cli.main()
        scheduler.status.assert_called_once_with()

        scheduler.reset_mock()
        scheduler.uninstall.return_value = True
        with mock.patch.object(cli, "Config", return_value=self.config), \
             mock.patch.object(cli, "MacOSQueueScheduler", return_value=scheduler), \
             mock.patch.object(sys, "argv", ["m3m", "queue", "uninstall-scheduler"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            cli.main()
        scheduler.uninstall.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
