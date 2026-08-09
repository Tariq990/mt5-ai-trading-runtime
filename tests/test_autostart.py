from __future__ import annotations

from types import SimpleNamespace

import gpttradder.autostart as autostart


def test_windows_autostart_builds_onlogon_task_and_launcher(tmp_path, monkeypatch):
    commands = []

    def fake_runner(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(autostart.platform, "system", lambda: "Windows")
    result = autostart.install_windows_autostart(
        root=tmp_path,
        python_executable=r"C:\Python\python.exe",
        runner=fake_runner,
    )
    launcher = tmp_path / "state" / "start-gpttradder-watchdog.cmd"
    assert launcher.exists()
    text = launcher.read_text(encoding="utf-8")
    assert "gpttradder.cli watchdog" in text
    assert commands[0][0].lower() == "schtasks.exe"
    assert "ONLOGON" in commands[0]
    assert autostart.TASK_NAME in commands[0]
    assert result["ok"] is True


def test_windows_autostart_remove_uses_delete(tmp_path, monkeypatch):
    commands = []

    def fake_runner(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(autostart.platform, "system", lambda: "Windows")
    result = autostart.remove_windows_autostart(runner=fake_runner)
    assert "/Delete" in commands[0]
    assert result["task"] == autostart.TASK_NAME
