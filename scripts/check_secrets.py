#!/usr/bin/env python3
"""Refuse to let API keys into git. Never prints a secret, only where it is.

    python scripts/check_secrets.py            # tracked + staged files on disk
    python scripts/check_secrets.py --staged   # what the next commit will hold
    python scripts/check_secrets.py --history  # also every commit, all branches

Checks:
1. Files that must stay private (.env, secret.key, the database, agent data)
   are git-ignored and not tracked.
2. No tracked file contains a value from the local `.env` (the exact keys
   you use), or anything shaped like an OpenAI or Anthropic key, or a key
   variable assigned a real-looking value.

Run with --history before pushing this project to a new or public remote.
`scripts/install-git-hooks.sh` runs the --staged check before every commit.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = (".env", ".env.local", "secret.key", "data/den.db", "den.db", "data/agents/x")

# Names that hold keys or key material wherever they sit in the tree. Checked
# against tracked paths directly too, so a `git add -f` or a weakened
# .gitignore cannot slip one past: secret.key is 32 random bytes that no
# content pattern can recognise, and with the database it decrypts every
# stored user key.
PRIVATE_NAME = re.compile(
    r"^(?:\.env(?:\..+)?|secret\.key|auth\.json)$"
    r"|\.(?:key|pem|db|db-wal|db-shm|sqlite|sqlite3)$",
    re.IGNORECASE,
)
PUBLIC_NAMES = {".env.example"}

MAX_FILE_BYTES = 5_000_000

_KEY_VARS = (r"(?:UMGPT_TOOLKIT_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
             r"DEN_UPSTREAM_API_KEY|CODEX_API_KEY|(?i:api_key))")

PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{40,}"),
    "OpenAI API key": re.compile(r"sk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"),
    # Shell and YAML assignments at the start of a line, quoted names (JSON,
    # dicts, `os.environ["..."] =`) and `api_key="..."` keyword arguments. A
    # bare NAME= inside a string or a sentence is not an assignment. Base64
    # keys may contain + / =.
    "key variable with a value": re.compile(
        rf"(?:^[ \t]*(?:export\s+|-\s+)?{_KEY_VARS}\s*[:=]\s*['\"]?"
        rf"|['\"]{_KEY_VARS}['\"]\]?\s*[:=]\s*['\"]?"
        rf"|\b(?i:api_key)\s*=\s*['\"])"
        r"([A-Za-z0-9_\-+/=]{16,})",
        re.MULTILINE,
    ),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

# Only these .env variables hold secrets. The rest (model lists, image tags,
# URLs, e-mail allow-lists) are settings that tracked files rightly repeat.
SECRET_VAR = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)$")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def git_paths(*args: str) -> list[str]:
    """Paths from a git listing, unquoted (-z), so unusual names are not skipped."""
    return [p for p in git(*args, "-z").split("\0") if p]


def is_private_name(path: str) -> bool:
    name = PurePosixPath(path).name
    return name not in PUBLIC_NAMES and bool(PRIVATE_NAME.search(name))


def _env_entries(path: Path) -> list[tuple[str, str]]:
    """(name, value) pairs from a .env file, read the way a dotenv loader does."""
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        raw, value = value, value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            # An unquoted "#" after whitespace (also right after "=") starts a
            # comment, as in den.config.dotenv_value: it is not part of the key.
            value = re.split(r"\s#", raw, maxsplit=1)[0].strip()
        entries.append((name, value))
    return entries


def local_secret_values() -> list[str]:
    """Values of the key variables in the local .env (long enough to be keys).

    Values equal to the documented default in .env.example are skipped: they
    are public by definition, and .env.example itself is tracked.
    """
    documented = dict(_env_entries(ROOT / ".env.example"))
    values = []
    for name, value in _env_entries(ROOT / ".env"):
        if (SECRET_VAR.search(name.upper()) and len(value) >= 16
                and value != documented.get(name)):
            values.append(value)
    return values


# Real keys are random: they mix digits and both cases. Readable placeholders
# in tests ("sk-upstream-must-never-reach-a-container") do not.
_KEY_SHAPED = {"Anthropic API key", "OpenAI API key", "key variable with a value"}


def _looks_random(value: str) -> bool:
    return (any(c.isdigit() for c in value) and any(c.isupper() for c in value)
            and any(c.islower() for c in value))


def _looks_like_key(name: str, value: str) -> bool:
    if name not in _KEY_SHAPED or _looks_random(value):
        return True
    # A value assigned to a named key variable needs less proof: hex
    # subscription keys (Azure / U-M Toolkit style) are single-case.
    return (name == "key variable with a value"
            and re.fullmatch(r"[0-9a-fA-F-]{20,}", value) is not None
            and any(c.isdigit() for c in value) and any(c.isalpha() for c in value))


def _matches(name: str, pattern: re.Pattern, text: str) -> bool:
    for match in pattern.finditer(text):
        if _looks_like_key(name, match.group(match.lastindex or 0)):
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
    # Any tracked file the ignore rules cover was force-added or tracked
    # before the rule existed; either way it is about to be published.
    bad = set(git_paths("ls-files", "--cached", "--ignored", "--exclude-standard"))
    bad |= {path for path in git_paths("ls-files") if is_private_name(path)}
    for path in sorted(bad):
        problems.append(f"{path} is tracked by git but must stay private; "
                        f"run: git rm --cached {path}")
    return problems


def check_files(secrets: list[str]) -> list[str]:
    """Scan tracked and staged files as they are on disk."""
    problems = []
    files = set(git_paths("ls-files")) | set(
        git_paths("diff", "--cached", "--name-only", "--diff-filter=ACM")
    )
    for name in sorted(files):
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for what in scan_text(text, secrets):
            problems.append(f"{name}: contains {what}")
    return problems


def check_staged(secrets: list[str]) -> list[str]:
    """Scan the index, which is what `git commit` records.

    The working tree can differ (`git add -p`, an edit or `rm` after `git
    add`), so the pre-commit hook must read the staged blobs, not the files.
    """
    blobs: dict[str, list[str]] = {}
    for entry in git_paths("ls-files", "--stage"):
        meta, path = entry.split("\t", 1)
        mode, sha, _stage = meta.split()
        if mode != "160000":  # a submodule commit, not a blob
            blobs.setdefault(sha, []).append(path)
    if not blobs:
        return []
    sizes = subprocess.run(
        ["git", "cat-file", "--batch-check"], cwd=ROOT, check=True,
        input="\n".join(blobs) + "\n", capture_output=True, text=True,
    ).stdout.split()
    small = [sha for sha, _type, size in zip(sizes[::3], sizes[1::3], sizes[2::3])
             if int(size) <= MAX_FILE_BYTES]
    out = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=ROOT, check=True,
        input=("\n".join(small) + "\n").encode(), capture_output=True,
    ).stdout
    problems = []
    pos = 0
    # --batch prints "<sha> <type> <size>\n<content>\n" per requested object.
    while pos < len(out):
        header_end = out.index(b"\n", pos)
        sha, _type, size = out[pos:header_end].decode().split()
        start = header_end + 1
        text = out[start:start + int(size)].decode(errors="ignore")
        pos = start + int(size) + 1
        found = scan_text(text, secrets)
        for path in blobs[sha]:
            problems.extend(f"{path} (staged): contains {what}" for what in found)
    return sorted(problems)


def check_history(secrets: list[str]) -> list[str]:
    problems = []
    # -m shows each merge's diff against every parent (git prints none for
    # merges by default), and --text gives the lines of binary files, so a
    # key added while resolving a merge or inside a binary blob is seen.
    log = subprocess.run(
        ["git", "log", "--all", "-p", "-m", "--text", "--format=commit %H"],
        cwd=ROOT, check=True, capture_output=True, text=True, errors="ignore",
    ).stdout
    commit = "?"
    for chunk in re.split(r"^(commit [0-9a-f]{40})(?: \(from [0-9a-f]{40}\))?$", log,
                          flags=re.MULTILINE):
        if chunk.startswith("commit "):
            commit = chunk[7:19]
            continue
        added = "\n".join(l[1:] for l in chunk.splitlines() if l.startswith("+"))
        for what in scan_text(added, secrets):
            problems.append(f"commit {commit}: adds {what}")
    # A key file deleted later is still in history, and its bytes match no
    # pattern, so look at the names of everything ever added.
    names = subprocess.run(
        ["git", "-c", "core.quotePath=false", "log", "--all", "-m", "--diff-filter=A",
         "--name-only", "--format=commit %H"],
        cwd=ROOT, check=True, capture_output=True, text=True, errors="ignore",
    ).stdout
    for line in names.splitlines():
        if re.fullmatch(r"commit [0-9a-f]{40}(?: \(from [0-9a-f]{40}\))?", line):
            commit = line[7:19]
        elif line and is_private_name(line):
            problems.append(f"commit {commit}: adds private file {line}")
    # A merge's diff against each parent repeats what the other side added.
    return list(dict.fromkeys(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", action="store_true", help="also scan every commit")
    parser.add_argument("--staged", action="store_true",
                        help="scan the staged content (what will be committed), "
                             "not the files on disk; the pre-commit hook uses this")
    args = parser.parse_args()
    secrets = local_secret_values()
    scan = check_staged if args.staged else check_files
    problems = check_ignored() + scan(secrets)
    if args.history:
        problems += check_history(secrets)
    if problems:
        print("Secret check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    scope = "staged files" if args.staged else "tracked files"
    if args.history:
        scope += " and history"
    print(f"Secret check passed ({scope}; {len(secrets)} local .env value(s) checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
