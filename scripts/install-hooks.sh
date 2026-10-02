#!/bin/sh
# One-time setup of the local privacy hooks (pre-commit, pre-push).
set -e
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
chmod +x .githooks/* scripts/privacy_check.py
echo "hooks enabled. Optional: put SHA-256 hashes of your personal words into .privacy-denylist"
echo "  (python3 scripts/privacy_check.py --hash 'First Last' >> .privacy-denylist)"
