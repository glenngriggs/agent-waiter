"""Regression tests for the secret scanner, its pre-commit hook and its docs.

Every scenario runs in a throwaway git repository that holds a copy of the
scanner, so the real repository's index, history and .env are never touched.
Key-shaped strings are assembled at runtime so this file itself stays clean.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts" / "check_secrets.py")
check_secrets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_secrets)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# Shaped like real keys (random-looking, right prefix), built so no literal
# in this file matches a pattern.
OPENAI_KEY = "sk-" + "proj-" + "Ab1Cd2Ef3Gh4" * 4
ANTHROPIC_KEY = "sk-" + "ant-api03-" + "Zy9Xw8Vu7Ts6" * 5
HEX_KEY = "0f3a9c" * 6 + "be12"

# Git must not pick up the user's own config (hooksPath, signing, templates).
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


class Repo:
    """A scratch git repo with the scanner, hook installer and ignore rules."""

    def __init__(self, path: Path) -> None:
        self.path = path
        (path / "scripts").mkdir(parents=True)
        for name in ("scripts/check_secrets.py", "scripts/install-git-hooks.sh",
                     ".gitignore", ".env.example"):
            shutil.copy(ROOT / name, path / name)
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.path, env=GIT_ENV, check=check,
                              capture_output=True, text=True)

    def write(self, name: str, content: str | bytes) -> None:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)

    def commit(self, name: str, content: str | bytes, message: str = "x") -> None:
        self.write(name, content)
        self.git("add", "-f", name)
        self.git("commit", "-q", "-m", message)

    def scan(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "scripts/check_secrets.py", *args],
                              cwd=self.path, env=GIT_ENV, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def _assert_no_secret_printed(result: subprocess.CompletedProcess) -> None:
    for key in (OPENAI_KEY, ANTHROPIC_KEY, HEX_KEY):
        assert key not in result.stdout + result.stderr


# --- #28: a .env copied from .env.example is not a leak ----------------------


def test_env_copied_from_example_passes(repo: Repo) -> None:
    shutil.copy(repo.path / ".env.example", repo.path / ".env")
    result = repo.scan()
    assert result.returncode == 0, result.stderr
    assert "0 local .env value(s)" in result.stdout


def test_settings_in_env_are_not_secrets_but_keys_are(repo: Repo) -> None:
    # A changed model list or image tag that tracked files repeat is fine; a
    # key variable's value (with an inline comment after it) is protected.
    plain_key = "plainlocalkeyvalue"  # long, but no digits: only the exact match finds it
    repo.write(".env", "DEN_MODELS=my-model-one,my-model-two\n"
                       f"UMGPT_TOOLKIT_API_KEY={plain_key}    # mine\n")
    repo.commit("notes.txt", "models: my-model-one,my-model-two\n")
    assert repo.scan().returncode == 0
    repo.commit("leak.txt", f"key is {plain_key}\n")
    result = repo.scan()
    assert result.returncode == 1
    assert "leak.txt: contains a value from your local .env" in result.stderr
    assert plain_key not in result.stdout + result.stderr


def test_local_values_with_a_slash_are_protected(repo: Repo) -> None:
    base64ish = "aB3/dE6+gH9/jK2+mN5="
    repo.write(".env", f'OPENAI_API_KEY="{base64ish}"\n')
    repo.commit("leak.txt", f"{base64ish}\n")
    assert "leak.txt: contains a value from your local .env" in repo.scan().stderr


# --- #29: the hook checks what is staged, not what is on disk ---------------


def test_hook_blocks_a_staged_key_changed_on_disk(repo: Repo) -> None:
    subprocess.run(["sh", "scripts/install-git-hooks.sh"], cwd=repo.path, env=GIT_ENV,
                   check=True, capture_output=True)
    repo.write("leak.txt", f"OPENAI={OPENAI_KEY}\n")
    repo.git("add", "leak.txt")
    repo.write("leak.txt", "clean\n")
    result = repo.git("commit", "-m", "x", check=False)
    assert result.returncode != 0
    assert "leak.txt (staged): contains OpenAI API key" in result.stderr
    _assert_no_secret_printed(result)
    assert repo.git("rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_hook_blocks_a_staged_key_deleted_on_disk(repo: Repo) -> None:
    subprocess.run(["sh", "scripts/install-git-hooks.sh"], cwd=repo.path, env=GIT_ENV,
                   check=True, capture_output=True)
    repo.write("leak.txt", f"K={ANTHROPIC_KEY}\n")
    repo.git("add", "leak.txt")
    (repo.path / "leak.txt").unlink()
    assert repo.git("commit", "-m", "x", check=False).returncode != 0


def test_hook_allows_clean_commits_and_ignores_unstaged_keys(repo: Repo) -> None:
    subprocess.run(["sh", "scripts/install-git-hooks.sh"], cwd=repo.path, env=GIT_ENV,
                   check=True, capture_output=True)
    repo.write("ok.txt", "clean\n")
    repo.git("add", "ok.txt")
    # Only the staged version is committed, so a key on disk does not block it.
    repo.write("ok.txt", f"{OPENAI_KEY}\n")
    assert repo.git("commit", "-m", "x", check=False).returncode == 0
    assert repo.git("show", "HEAD:ok.txt").stdout == "clean\n"


# --- #30: history includes merge commits and binary files -------------------


def test_history_sees_a_key_added_in_a_merge(repo: Repo) -> None:
    repo.git("checkout", "-q", "-b", "side")
    repo.commit("side.txt", "side\n")
    repo.git("checkout", "-q", "main")
    repo.commit("main.txt", "main\n")
    repo.git("merge", "-q", "--no-commit", "--no-ff", "side")
    repo.write("m.txt", f"K={ANTHROPIC_KEY}\n")
    repo.git("add", "m.txt")
    repo.git("commit", "-q", "-m", "merge")
    merge = repo.git("rev-parse", "HEAD").stdout.strip()[:12]
    repo.git("rm", "-q", "m.txt")
    repo.git("commit", "-q", "-m", "remove")
    result = repo.scan("--history")
    assert result.returncode == 1
    assert f"commit {merge}: adds Anthropic API key" in result.stderr
    _assert_no_secret_printed(result)


def test_history_sees_a_key_in_a_deleted_binary_file(repo: Repo) -> None:
    repo.commit("blob.bin", b"\x00\x01header\n" + ANTHROPIC_KEY.encode() + b"\n")
    repo.git("rm", "-q", "blob.bin")
    repo.git("commit", "-q", "-m", "remove")
    result = repo.scan("--history")
    assert result.returncode == 1
    assert "adds Anthropic API key" in result.stderr


# --- #32: key variables in any common syntax, including hex keys ------------


@pytest.mark.parametrize("text", [
    f"UMGPT_TOOLKIT_API_KEY={HEX_KEY}\n",
    f"export ANTHROPIC_API_KEY={HEX_KEY.upper()}\n",
    f'OPENAI_API_KEY: "{OPENAI_KEY[8:]}"\n',
    '{"UMGPT_TOOLKIT_API_KEY": "%s"}' % OPENAI_KEY[8:],
    'client = OpenAI(api_key="%s")' % HEX_KEY,
    'os.environ["DEN_UPSTREAM_API_KEY"] = "%s"' % HEX_KEY,
    "  - CODEX_API_KEY=%s\n" % HEX_KEY,
])
def test_key_variable_forms_are_caught(text: str) -> None:
    assert check_secrets.scan_text(text, []) == ["key variable with a value"]


@pytest.mark.parametrize("text", [
    "UMGPT_TOOLKIT_API_KEY=...\n",
    "DEN_UPSTREAM_API_KEY=fake\n",
    '{"api_key": "sk-ant-wrong-key-000"}',
    "api_key=settings.upstream_api_key_value",
    'X = "sk-upstream-must-never-reach-a-container-ok"',
    "set OPENAI_API_KEY=your-key-goes-here-please in .env",
])
def test_placeholders_are_not_keys(text: str) -> None:
    assert check_secrets.scan_text(text, []) == []


# --- #33: private files are reported wherever they are tracked --------------


def test_force_added_key_file_and_database_are_reported(repo: Repo) -> None:
    repo.commit("data/secret.key", os.urandom(32))
    repo.commit("data/den.db", b"SQLite format 3\x00")
    result = repo.scan()
    assert result.returncode == 1
    assert "data/secret.key is tracked by git but must stay private" in result.stderr
    assert "data/den.db is tracked by git but must stay private" in result.stderr


def test_private_file_names_are_reported_even_if_not_ignored(repo: Repo) -> None:
    repo.write(".gitignore", "")
    repo.git("add", ".gitignore")
    repo.commit("config/.env.local", "X=1\n")
    assert "config/.env.local is tracked by git" in repo.scan().stderr


def test_history_reports_a_deleted_key_file(repo: Repo) -> None:
    repo.commit("data/secret.key", os.urandom(32), "add")
    added = repo.git("rev-parse", "HEAD").stdout.strip()[:12]
    repo.git("rm", "-q", "data/secret.key")
    repo.git("commit", "-q", "-m", "remove")
    assert repo.scan().returncode == 0
    result = repo.scan("--history")
    assert result.returncode == 1
    assert f"commit {added}: adds private file data/secret.key" in result.stderr


def test_env_example_is_allowed() -> None:
    assert not check_secrets.is_private_name(".env.example")
    assert check_secrets.is_private_name("deploy/.env")
    assert check_secrets.is_private_name("x/secret.key")


# --- #31 (README part) and #34: the docs say what actually works ------------


def test_readme_env_lines_have_no_inline_comments() -> None:
    readme = (ROOT / "README.md").read_text()
    blocks = re.findall(r"```sh\n(.*?)```", readme, flags=re.DOTALL)
    for block in blocks:
        for line in block.splitlines():
            if re.match(r"[A-Z][A-Z0-9_]*=", line):
                assert not re.search(r"\s#", line), line
