"""User-managed macOS LaunchAgent for processing the local session queue."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
from typing import Callable, Dict, Optional

from .config import Config


DEFAULT_INTERVAL_SECONDS = 300
LABEL_PREFIX = "com.agent-m3m.queue-processor"


class SchedulerError(RuntimeError):
    """Raised when a per-project queue scheduler cannot be managed safely."""


@dataclass(frozen=True)
class SchedulerStatus:
    """Read-only view of one project-specific LaunchAgent."""

    label: str
    plist_path: Path
    installed: bool
    interval_seconds: Optional[int] = None
    executable: Optional[str] = None
    loaded: Optional[bool] = None


class MacOSQueueScheduler:
    """Install one explicit, per-project LaunchAgent queue worker.

    This scheduler is deliberately separate from Codex lifecycle hooks. Hooks
    only enqueue SessionEnd events; launchd later invokes ``m3m queue process``.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        home_dir: Optional[Path] = None,
        executable: Optional[str] = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.config = config or Config()
        self.home_dir = Path(home_dir).expanduser().resolve() if home_dir else Path.home().resolve()
        self.executable = executable
        self.runner = runner

    @property
    def project_root(self) -> Path:
        root = self.config.project_root
        if not root:
            raise SchedulerError("no project memory directory is configured")
        return root

    @property
    def label(self) -> str:
        project_id = self.config.project_id
        if not project_id:
            raise SchedulerError("no project identifier is available")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", project_id)
        return f"{LABEL_PREFIX}.{safe_id}"

    @property
    def plist_path(self) -> Path:
        return self.home_dir / "Library" / "LaunchAgents" / f"{self.label}.plist"

    @staticmethod
    def _validate_interval(interval_seconds: int) -> int:
        try:
            interval = int(interval_seconds)
        except (TypeError, ValueError):
            raise SchedulerError("interval must be a positive number of seconds")
        if interval < 30:
            raise SchedulerError("interval must be at least 30 seconds")
        return interval

    def _resolve_executable(self) -> str:
        executable = self.executable or shutil.which("m3m")
        if not executable:
            raise SchedulerError("could not find the 'm3m' executable in PATH")
        return str(Path(executable).expanduser().resolve())

    def _plist_payload(self, interval_seconds: int, executable: str) -> Dict[str, object]:
        return {
            "Label": self.label,
            "ProgramArguments": [executable, "queue", "process", "--limit", "10"],
            "WorkingDirectory": str(self.project_root),
            "StartInterval": interval_seconds,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
        }

    @staticmethod
    def _read_plist(path: Path) -> Optional[Dict[str, object]]:
        try:
            with path.open("rb") as handle:
                data = plistlib.load(handle)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None

    def _is_loaded(self) -> Optional[bool]:
        """Return launchd state with a read-only query when available."""
        domain = f"gui/{os.getuid()}"
        service = f"{domain}/{self.label}"
        try:
            result = self.runner(
                ["launchctl", "print", service],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        return result.returncode == 0

    def status(self) -> SchedulerStatus:
        """Return plist and loaded-service state without changing scheduler state."""
        path = self.plist_path
        data = self._read_plist(path)
        if not data or data.get("Label") != self.label:
            return SchedulerStatus(label=self.label, plist_path=path, installed=False)
        arguments = data.get("ProgramArguments")
        executable = arguments[0] if isinstance(arguments, list) and arguments and isinstance(arguments[0], str) else None
        interval = data.get("StartInterval")
        return SchedulerStatus(
            label=self.label,
            plist_path=path,
            installed=True,
            interval_seconds=interval if isinstance(interval, int) else None,
            executable=executable,
            loaded=self._is_loaded(),
        )

    def install(self, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> SchedulerStatus:
        """Write and load the exact per-project plist, safely replacing itself."""
        interval = self._validate_interval(interval_seconds)
        executable = self._resolve_executable()
        payload = self._plist_payload(interval, executable)
        path = self.plist_path
        path.parent.mkdir(parents=True, exist_ok=True)
        plist_bytes = plistlib.dumps(payload, sort_keys=True)
        try:
            with path.open("wb") as handle:
                handle.write(plist_bytes)
        except OSError as exc:
            raise SchedulerError(f"could not write scheduler plist '{path}': {exc}") from exc

        domain = f"gui/{os.getuid()}"
        service = f"{domain}/{self.label}"
        # It is normal for a first install to have no loaded service. Ignore
        # that result, then bootstrap only this exact label.
        self.runner(["launchctl", "bootout", service], check=False, capture_output=True, text=True)
        try:
            self.runner(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            removed = False
            try:
                if path.read_bytes() == plist_bytes:
                    path.unlink()
                    removed = True
            except OSError as cleanup_exc:
                raise SchedulerError(
                    f"could not load scheduler '{self.label}': {exc}; "
                    f"could not remove the plist written for this attempt: {cleanup_exc}"
                ) from exc
            cleanup = "plist removed" if removed else "plist was changed before cleanup and was left untouched"
            raise SchedulerError(f"could not load scheduler '{self.label}': {exc}; {cleanup}") from exc
        return self.status()

    def uninstall(self) -> bool:
        """Unload and remove only this project's exact scheduler plist."""
        path = self.plist_path
        data = self._read_plist(path)
        if not data:
            return False
        if data.get("Label") != self.label:
            raise SchedulerError(f"refusing to remove unexpected plist at '{path}'")
        domain = f"gui/{os.getuid()}"
        service = f"{domain}/{self.label}"
        self.runner(["launchctl", "bootout", service], check=False, capture_output=True, text=True)
        try:
            path.unlink()
        except OSError as exc:
            raise SchedulerError(f"could not remove scheduler plist '{path}': {exc}") from exc
        return True
