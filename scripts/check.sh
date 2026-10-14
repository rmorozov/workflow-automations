#!/usr/bin/env sh
# Run in a disposable validation worktree; generated caches/build files stay there.
set -eu
cd "$(dirname "$0")/.."
ruff check .
ruff format --check .
python scripts/backlog.py check
python scripts/agent_profiles.py check
pytest -q
python -m build
