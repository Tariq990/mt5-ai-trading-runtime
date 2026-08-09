from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

TASK_NAME = "GPTTRADDER-Watchdog"


def project_root() -> Path:
    # Editable install / source checkout: src/gpttradder/autostart.py -> repo root.
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").exists():
        return candidate
    return Path.cwd().resolve()


def write_windows_launcher(root: Path | None = None, python_executable: str | None = None) -> Path:
    root = (root or project_root()).resolve()
    python_executable = python_executable or sys.executable
    state = root / "state"
    logs = root / "logs"
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    launcher = state / "start-gpttradder-watchdog.cmd"
    launcher.write_text(
        "@echo off\r\n"
        f'cd /d "{root}"\r\n'
        f'"{python_executable}" -m gpttradder.cli watchdog >> "{logs / "watchdog-autostart.log"}" 2>&1\r\n',
        encoding="utf-8",
    )
    return launcher


def install_windows_autostart(
    *,
    root: Path | None = None,
    python_executable: str | None = None,
    runner=subprocess.run,
) -> dict:
    if platform.system() != "Windows":
        raise RuntimeError("Windows Task Scheduler autostart is only available on Windows")
    launcher = write_windows_launcher(root, python_executable)
    command = [
        "schtasks.exe",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        str(launcher),
        "/SC",
        "ONLOGON",
        "/F",
    ]
    completed = runner(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"schtasks /Create failed ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return {"ok": True, "task": TASK_NAME, "launcher": str(launcher), "command": command}


def remove_windows_autostart(*, runner=subprocess.run) -> dict:
    if platform.system() != "Windows":
        raise RuntimeError("Windows Task Scheduler autostart is only available on Windows")
    command = ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"]
    completed = runner(command, capture_output=True, text=True, check=False)
    # ERROR_FILE_NOT_FOUND style output should still be transparent to the user.
    if completed.returncode != 0:
        raise RuntimeError(
            f"schtasks /Delete failed ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return {"ok": True, "task": TASK_NAME, "command": command}
