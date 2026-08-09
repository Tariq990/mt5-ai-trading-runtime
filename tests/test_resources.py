from __future__ import annotations

import hashlib
from pathlib import Path

from gpttradder.resources import bridge_directory


def test_bridge_assets_are_present():
    path = bridge_directory()
    assert (path / "server.mjs").exists()
    assert (path / "package.json").exists()
    assert (path / "json.mjs").exists()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_decision_prompt_copies_are_hash_synced():
    root = Path(__file__).resolve().parents[1]
    packaged = root / "src" / "gpttradder" / "assets" / "prompts" / "chatgpt_decision_prompt.md"
    mirrored = root / "prompts" / "chatgpt_decision_prompt.md"
    assert packaged.exists() and mirrored.exists()
    assert _sha256(packaged) == _sha256(mirrored), (
        "prompts/chatgpt_decision_prompt.md must mirror the packaged prompt (hash-synced per AGENTS.md)"
    )
