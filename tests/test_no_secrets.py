"""The repository holds no API keys, and private files stay git-ignored."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts" / "check_secrets.py")
check_secrets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_secrets)

pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")


def test_private_files_are_ignored() -> None:
    assert check_secrets.check_ignored() == []


def test_no_tracked_file_contains_a_key() -> None:
    assert check_secrets.check_files(check_secrets.local_secret_values()) == []


def test_the_scanner_catches_keys() -> None:
    fake_anthropic = "sk-ant-api03-" + "A1b2C3d4" * 6
    assert check_secrets.scan_text(f"key = '{fake_anthropic}'", []) == ["Anthropic API key"]
    assert check_secrets.scan_text("OPENAI_API_KEY=Abcdefgh1jklmnopqrstuvwx", []) != []
    assert check_secrets.scan_text('X = "sk-upstream-must-never-reach-a-container-ok"', []) == []
    assert check_secrets.scan_text("UMGPT_TOOLKIT_API_KEY=\n", []) == []
    assert check_secrets.scan_text("x my-local-value-123456 y", ["my-local-value-123456"]) == [
        "a value from your local .env"]
