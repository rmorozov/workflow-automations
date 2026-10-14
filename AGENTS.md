# Working in this repository

- Keep tools local and deterministic unless their specification explicitly requires networking.
- Implement tools in `src/workflow_automations/`; keep script wrappers thin.
- Update the current specification and README when behavior changes.
- Use synthetic data; never commit workplace exports, translation bundles, or credentials.
- Add behavior tests for joins, validation, IO boundaries and meaningful regressions.
- Run `ruff check .`, `ruff format --check .`, `pytest`, and `python -m build` before a PR.
- Keep dependency extras per tool. Add the extras needed by new tests to CI installation.
- Preserve input files. Overwriting outputs must be explicit and documented.
- Prefer a focused PR; keep deferred features as task files in `docs/backlog/`.
- Edit only the files and spec sections your task's `touches:` lists; parallel branches rely on it.
- Get new backlog IDs from the coordinator; delete a task file in the PR that completes it.
- Roles, models and the parallel-work rules are in `docs/agentic-workflow.md`.

- Backlog tasks use neutral `tier`; resolve Codex roles with `scripts/agent_profiles.py route`.
- Codex profiles are in `.codex/agents/`; do not treat static validation as runtime activation.
- Delegate only when useful: at most two editing workers, each in an isolated worktree;
  the coordinator owns integration and remote publication. Fresh-context review roles do not edit.
- Use `scripts/check.sh` for the shared checks in a disposable validation worktree.
