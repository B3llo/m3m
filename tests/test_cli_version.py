"""Regression tests for the shared agent-m3m CLI version option."""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_m3m import __version__
from agent_m3m import cli


class TestCliVersion(unittest.TestCase):
    def test_version_uses_invoked_console_script_name_without_initializing_config(self):
        for executable in ("m3m", "agent-m3m"):
            with self.subTest(executable=executable):
                output = io.StringIO()
                with mock.patch.object(cli.sys, "argv", [executable, "--version"]), \
                     mock.patch.object(cli, "Config") as config_class, \
                     redirect_stdout(output):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main()

                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(output.getvalue().strip(), f"{executable} {__version__}")
                config_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
