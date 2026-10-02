#!/usr/bin/env python3
"""Privacy and safety gate for this public repo (stdlib only).

Looks for personal data and secrets in the lines a change adds, so a sync from a
private copy or a pasted statement cannot reach the public repository.

    privacy_check.py --staged          # what is about to be committed
    privacy_check.py --base origin/master   # everything a branch/PR adds
    privacy_check.py --all             # the whole tracked tree
    privacy_check.py --hash "Some Name"     # print denylist entries for a name

Checks
- secrets: Telegram bot tokens, API keys, private keys, DB URLs with real credentials;
- personal data: e-mails, deployed Railway domains, home-directory paths, long digit
  runs (account numbers), phone numbers;
- files that must never be public: statements (pdf/csv outside tests/fixtures), .env,
  keys, private-only folders;
- workflows: `pull_request_target` and piping downloads into a shell;
- denylist: personal words (names, handles) kept only as SHA-256 hashes, so the
  list itself leaks nothing. Hashes come from the PRIVACY_DENYLIST env var (CI secret)
  or the git-ignored file .privacy-denylist, one hash per line.

A line can opt out with the comment `privacy: ok` (use for placeholders).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = {"scripts/privacy_check.py", "tests/test_privacy_check.py"}
PRAGMA = "privacy: ok"

FORBIDDEN_PATHS = [
    (re.compile(r"(^|/)\.env($|\.(?!example$))"), "env file with real values"),
    (re.compile(r"\.(pem|key|p12|pfx)$", re.I), "key material"),
    (re.compile(r"\.(pdf|xlsx?)$", re.I), "statement-like document"),
    (re.compile(r"\.csv$", re.I), "csv outside tests/fixtures"),
    (re.compile(r"^(\.railway|integrations/(gmail-apps-script|receipts-routine))/"), "private-only folder"),
    (re.compile(r"^CLAUDE\.md$"), "private project notes"),
]
FIXTURE_OK = re.compile(r"^tests/fixtures/")

PLACEHOLDER = re.compile(r"(<[^>]+>|\$\{\{|\bexample\b|\byour[-_ ]|\bchangeme\b|\bxxx+\b|\.\.\.)", re.I)

LINE_PATTERNS = [
    ("telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("API key", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16})\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("database url with credentials", re.compile(r"postgres(?:ql)?(?:\+\w+)?://(?!finance:finance@)[^\s:/@$]+:[^\s@$]+@(?!localhost|127\.0\.0\.1|\$)")),
    ("deployed Railway domain", re.compile(r"\b[a-z0-9-]+\.up\.railway\.app\b", re.I)),
    ("e-mail address", re.compile(r"\b[\w.+-]+@(?!example\.|users\.noreply\.github\.com|noreply\.)[\w-]+\.[a-z]{2,}\b", re.I)),
    ("home-directory path", re.compile(r"(?:/Users/(?!<)[\w.-]+|/home/(?!user\b|runner\b|<)[\w.-]+|~/(?:Desktop|Documents|Downloads)/)")),
    # long digit runs: placeholders (all-same digits, a date prefix, "000" blocks) are fine
    ("long digit run (account / card / phone?)", re.compile(r"(?<![\w.])(?!(\d)\1+\b)(?!20[23]\d[01]\d[0-3]\d)(?!\d*000)\d{9,}(?![\w.])")),
    ("phone number", re.compile(r"(?<![\w\d])\+(?:60|7|61|65)[\s-]?\d[\d\s-]{7,12}\d")),
]
WORKFLOW_PATTERNS = [
    ("workflow runs with secrets on untrusted code", re.compile(r"\bpull_request_target\b")),
    ("workflow pipes a download into a shell", re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b")),
]
WORD = re.compile(r"[^\W\d_]{3,}|\w{3,}", re.UNICODE)


def word_hash(word: str) -> str:
    return hashlib.sha256(word.lower().encode()).hexdigest()


def load_denylist() -> set[str]:
    raw = os.environ.get("PRIVACY_DENYLIST", "")
    path = ROOT / ".privacy-denylist"
    if path.exists():
        raw += "\n" + path.read_text()
    return {h for h in re.split(r"[\s,]+", raw.strip().lower()) if re.fullmatch(r"[0-9a-f]{64}", h)}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def added_lines(args: argparse.Namespace) -> tuple[list[str], dict[str, list[tuple[int, str]]]]:
    """(paths of added/changed files, {path: [(lineno, text), ...]} of added lines)."""
    if args.all:
        paths = [p for p in git("ls-files").split("\n") if p]
        out = {}
        for p in paths:
            f = ROOT / p
            if f.is_file():
                try:
                    out[p] = list(enumerate(f.read_text().splitlines(), 1))
                except UnicodeDecodeError:
                    pass
        return paths, out
    diff_args = ["diff", "--cached"] if args.staged else ["diff", f"{git('merge-base', args.base, 'HEAD').strip()}..HEAD"]
    paths = [p for p in git(*diff_args, "--name-only", "--diff-filter=AMR").split("\n") if p]
    out: dict[str, list[tuple[int, str]]] = {}
    path, lineno = None, 0
    for line in git(*diff_args, "-U0", "--no-color", "--diff-filter=AMR").split("\n"):
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            lineno = int(re.search(r"\+(\d+)", line).group(1)) - 1
        elif line.startswith("+") and path:
            lineno += 1
            out.setdefault(path, []).append((lineno, line[1:]))
    return paths, out


def check(paths: list[str], lines: dict[str, list[tuple[int, str]]], denylist: set[str]) -> list[str]:
    problems: list[str] = []
    for p in paths:
        if p in SELF:
            continue
        for rx, why in FORBIDDEN_PATHS:
            if rx.search(p) and not (FIXTURE_OK.match(p) and p.endswith(".csv")):
                problems.append(f"{p}: {why} must not be in the public repo")
    for p, rows in lines.items():
        if p in SELF:
            continue
        is_workflow = p.startswith(".github/workflows/")
        for no, text in rows:
            if PRAGMA in text:
                continue
            for name, rx in LINE_PATTERNS:
                if p.startswith("tests/") and name == "phone number":
                    continue  # statement fixtures carry bank and company phone lines
                m = rx.search(text)
                if m and p.startswith("tests/") and name.startswith("long digit") and len(m.group(0)) < 12:
                    continue  # 9-11 digits: transaction references in statement fixtures
                if m and not PLACEHOLDER.search(text):
                    problems.append(f"{p}:{no}: {name}: {m.group(0)[:40]!r}")
            if is_workflow:
                for name, rx in WORKFLOW_PATTERNS:
                    if rx.search(text):
                        problems.append(f"{p}:{no}: {name}")
            if denylist:
                for w in WORD.findall(text):
                    if word_hash(w) in denylist:
                        problems.append(f"{p}:{no}: a word from the private denylist (hash {word_hash(w)[:8]}…)")
    return problems


GUARDED_BASH = re.compile(r"\bgit\s+push\b|\bgh\s+pr\s+(create|merge)\b")
GUARDED_TOOLS = re.compile(r"^mcp__github__(create_pull_request|merge_pull_request|push_files|create_or_update_file)$")


def run_hook() -> int:
    """PreToolUse hook: block a push / PR / merge while the change fails the check (exit 2)."""
    import json

    event = json.load(sys.stdin)
    tool, tin = event.get("tool_name", ""), event.get("tool_input") or {}
    problems: list[str] = []
    if tool == "Bash" and GUARDED_BASH.search(tin.get("command", "")):
        base = next((r for r in ("origin/master", "origin/main") if subprocess.run(["git", "rev-parse", "--verify", "-q", r], cwd=ROOT, capture_output=True).returncode == 0), None)
        if base:
            paths, lines = added_lines(argparse.Namespace(all=False, staged=False, base=base))
            problems = check(paths, lines, load_denylist())
    elif GUARDED_TOOLS.match(tool):
        # content sent straight to GitHub never touches the working tree: scan the payload
        text = json.dumps(tin, ensure_ascii=False).replace("\\n", "\n")
        problems = check([], {"<payload>": list(enumerate(text.split("\n"), 1))}, load_denylist())
    if problems:
        print("Privacy check blocked this action:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--base", metavar="REF", help="compare HEAD with the merge base of REF")
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--hash", metavar="TEXT", help="print SHA-256 denylist entries for each word of TEXT")
    mode.add_argument("--hook", action="store_true", help="Claude Code PreToolUse hook: JSON on stdin")
    args = ap.parse_args(argv)
    if args.hash:
        for w in sorted({w.lower() for w in WORD.findall(args.hash)}):
            print(word_hash(w), file=sys.stdout)
        return 0
    if args.hook:
        return run_hook()
    if not (args.staged or args.base or args.all):
        ap.error("pick --staged, --base REF or --all")
    denylist = load_denylist()
    paths, lines = added_lines(args)
    problems = check(paths, lines, denylist)
    scope = "tree" if args.all else "staged changes" if args.staged else f"changes since {args.base}"
    if not denylist:
        print("note: no personal denylist loaded (PRIVACY_DENYLIST / .privacy-denylist); generic checks only", file=sys.stderr)
    if problems:
        print(f"privacy check FAILED for {scope}:", file=sys.stderr)
        for pr in problems:
            print(f"  {pr}", file=sys.stderr)
        print(f"Fix it, or mark a harmless placeholder line with the comment '{PRAGMA}'.", file=sys.stderr)
        return 1
    print(f"privacy check passed for {scope} ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
