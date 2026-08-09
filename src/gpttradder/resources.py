from __future__ import annotations

from pathlib import Path


def source_root() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    return candidate if (candidate / "pyproject.toml").exists() else Path.cwd().resolve()


def bridge_directory() -> Path:
    repo = source_root() / "bridge"
    if (repo / "server.mjs").exists():
        return repo
    packaged = Path(__file__).resolve().parent / "assets" / "bridge"
    if (packaged / "server.mjs").exists():
        return packaged
    raise RuntimeError("GPTTRADDER Node bridge assets are missing")
