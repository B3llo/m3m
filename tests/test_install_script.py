"""Safety checks for the isolated per-user installer."""

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT_ROOT / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def _write_fake_tool(self, bin_dir: Path, name: str) -> None:
        tool = bin_dir / name
        tool.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$0" "$@" > "$INSTALLER_CAPTURE"\n',
            encoding="utf-8",
        )
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)

    def _run_installer(self, available_tools: tuple[str, ...]):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for tool in available_tools:
                self._write_fake_tool(bin_dir, tool)

            capture = root / "capture.txt"
            env = {
                "INSTALLER_CAPTURE": str(capture),
                "PATH": f"{bin_dir}:/usr/bin:/bin",
            }
            result = subprocess.run(
                ["/bin/bash", str(INSTALLER)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            captured = capture.read_text(encoding="utf-8") if capture.exists() else ""
            return result, captured

    def test_prefers_uv_and_only_installs_the_local_project(self):
        result, captured = self._run_installer(("uv", "pipx"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured.splitlines()[1:],
            ["tool", "install", "--force", str(PROJECT_ROOT)],
        )
        self.assertTrue(captured.splitlines()[0].endswith("/uv"))
        self.assertNotIn("init", captured)
        self.assertNotIn("doctor", captured)

    def test_falls_back_to_pipx(self):
        result, captured = self._run_installer(("pipx",))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured.splitlines()[1:],
            ["install", "--force", str(PROJECT_ROOT)],
        )
        self.assertTrue(captured.splitlines()[0].endswith("/pipx"))

    def test_fails_without_an_isolated_tool_installer(self):
        result, captured = self._run_installer(())

        self.assertEqual(result.returncode, 1)
        self.assertEqual(captured, "")
        self.assertIn("install uv or pipx first", result.stderr)

    def test_script_contains_no_system_python_or_automatic_initialization(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertNotIn("--break-system-packages", script)
        self.assertNotIn("pip3 install", script)
        self.assertNotIn("m3m init --global\n", script)
        self.assertNotIn("agent_m3m.cli init", script)


if __name__ == "__main__":
    unittest.main()
