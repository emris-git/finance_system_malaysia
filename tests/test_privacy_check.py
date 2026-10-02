"""The privacy gate: what it must catch and what it must let through."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import privacy_check as pc  # noqa: E402


def run(rows, paths=("a.py",), deny=()):
    return pc.check(list(paths), {p: list(enumerate(r, 1)) for p, r in rows.items()}, {pc.word_hash(w) for w in deny})


def test_catches_secrets_and_personal_data():
    bad = [
        "BOT = '123456789:" + "A" * 35 + "'",
        "KEY = 'sk-ant-api03-abcdefghijkl'",
        "url = 'https://shop-prod.up.railway.app'",
        "mail = 'someone@gmail.com'",
        "p = '/Users/bob/Desktop'",
        "acct = '8001234567890'",
        "DB = 'postgresql://admin:hunter2@db.prod-host.net/x'",
    ]
    for line in bad:
        assert run({"a.py": [line]}), line


def test_lets_placeholders_through():
    ok = [
        "ACC = '00000001'",
        "url = 'https://<service>.up.railway.app'",
        "mail = 'me@example.com'",
        "DB = 'postgresql://finance:finance@localhost:5432/finance'",
        "DB = 'postgresql://u:p@${{Postgres.PGHOST}}/x'",
        "ref = '202608011112128001001700000000'",
        "Maybank 8001234567890  # privacy: ok",
    ]
    for line in ok:
        assert not run({"a.py": [line]}), line


def test_denylist_matches_words_by_hash_only():
    assert run({"a.py": ["hello Morgan!"]}, deny=["morgan"])
    assert not run({"a.py": ["hello Morganite"]}, deny=["morgan"])
    assert pc.word_hash("MORGAN") == pc.word_hash("morgan")


def test_forbidden_files_and_workflows():
    assert run({}, paths=["statement.pdf"])
    assert run({}, paths=[".env"])
    assert not run({}, paths=[".env.example", "tests/fixtures/sample.csv"])
    assert run({}, paths=["export.csv"])
    assert run({".github/workflows/x.yml": ["on: pull_request_target"]}, paths=[])


def test_hook_blocks_a_payload_with_personal_data():
    event = {"tool_name": "mcp__github__create_or_update_file", "tool_input": {"content": "owner: me@gmail.com"}}
    r = subprocess.run([sys.executable, str(Path(pc.__file__))] + ["--hook"], input=json.dumps(event), capture_output=True, text=True)
    assert r.returncode == 2 and "e-mail" in r.stderr
    event["tool_input"]["content"] = "nothing personal"
    r = subprocess.run([sys.executable, str(Path(pc.__file__))] + ["--hook"], input=json.dumps(event), capture_output=True, text=True)
    assert r.returncode == 0
