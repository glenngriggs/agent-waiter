#!/usr/bin/env python3
"""Refuse to let API keys into git. Never prints a secret, only where it is.

    python scripts/check_secrets.py            # tracked + staged files
    python scripts/check_secrets.py --history  # also every commit, all branches

Checks:
1. Files that must stay private (.env, secret.key, the database, agent data)
   are git-ignored and not tracked.
2. No tracked file contains a value from the local `.env` (the exact keys
   you use), or anything shaped like an OpenAI or Anthropic key, or a key
   variable assigned a real-looking value.

Run with --history before pushing this project to a new or public remote.
`scripts/install-git-hooks.sh` runs the quick check before every commit.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = (".env", ".env.local", "secret.key", "data/den.db", "den.db", "data/agents/x")

PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{40,}"),
    "OpenAI API key": re.compile(r"sk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"),
    "key variable with a value": re.compile(
        r"^\s*(?:export\s+)?(?:UMGPT_TOOLKIT_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
        r"DEN_UPSTREAM_API_KEY|CODEX_API_KEY)\s*=\s*['\"]?([A-Za-z0-9_\-]{16,})",
        re.MULTILINE,
    ),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def local_secret_values() -> list[str]:
    """Values from the local .env worth protecting (long enough to be keys)."""
    env = ROOT / ".env"
    if not env.is_file():
        return []
    values = []
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip().strip("'\"")
        if len(value) >= 16 and "@" not in value and "/" not in value:
            values.append(value)
    return values


# Real keys are random: they mix digits and both cases. Readable placeholders
# in tests ("sk-upstream-must-never-reach-a-container") do not.
_KEY_SHAPED = {"Anthropic API key", "OpenAI API key", "key variable with a value"}


def _looks_random(value: str) -> bool:
    return (any(c.isdigit() for c in value) and any(c.isupper() for c in value)
            and any(c.islower() for c in value))


def _matches(name: str, pattern: re.Pattern, text: str) -> bool:
    for match in pattern.finditer(text):
        value = match.group(match.lastindex or 0)
        if name not in _KEY_SHAPED or _looks_random(value):
            return True
    return False


def scan_text(text: str, secrets: list[str]) -> list[str]:
    found = [name for name, pattern in PATTERNS.items() if _matches(name, pattern, text)]
    if any(secret in text for secret in secrets):
        found.append("a value from your local .env")
    return found


def check_ignored() -> list[str]:
    problems = []
    for path in MUST_BE_IGNORED:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT
        ).returncode == 0
        if not ignored:
            problems.append(f"{path} is not git-ignored")
    tracked = set(git("ls-files").splitlines())
    for path in (".env", "secret.key"):
        if path in tracked:
            problems.append(f"{path} is tracked by git; run: git rm --cached {path}")
    return problems


def check_files(secrets: list[str]) -> list[str]:
    problems = []
    files = set(git("ls-files").splitlines()) | set(
        git("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines()
    )
    for name in sorted(files):
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for what in scan_text(text, secrets):
            problems.append(f"{name}: contains {what}")
    return problems


def check_history(secrets: list[str]) -> list[str]:
    problems = []
    log = subprocess.run(
        ["git", "log", "--all", "-p", "--format=commit %H"], cwd=ROOT, check=True,
        capture_output=True, text=True, errors="ignore",
    ).stdout
    commit = "?"
    for chunk in re.split(r"^(commit [0-9a-f]{40})$", log, flags=re.MULTILINE):
        if chunk.startswith("commit "):
            commit = chunk[7:19]
            continue
        added = "\n".join(l[1:] for l in chunk.splitlines() if l.startswith("+"))
        for what in scan_text(added, secrets):
            problems.append(f"commit {commit}: adds {what}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", action="store_true", help="also scan every commit")
    args = parser.parse_args()
    secrets = local_secret_values()
    problems = check_ignored() + check_files(secrets)
    if args.history:
        problems += check_history(secrets)
    if problems:
        print("Secret check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    scope = "tracked files and history" if args.history else "tracked files"
    print(f"Secret check passed ({scope}; {len(secrets)} local .env value(s) checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
