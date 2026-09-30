# Working in this repository

- Keep tools local and deterministic unless their specification explicitly requires networking.
- Implement tools in `src/workflow_automations/`; keep script wrappers thin.
- Update the current specification and README when behavior changes.
- Use synthetic data; never commit workplace exports, translation bundles, or credentials.
- Add behavior tests for joins, validation, IO boundaries and meaningful regressions.
- Run `ruff check .`, `ruff format --check .`, `pytest`, and `python -m build` before a PR.
- Keep dependency extras per tool. Add the extras needed by new tests to CI installation.
- Preserve input files. Overwriting outputs must be explicit and documented.
- Prefer a focused PR; keep deferred features in `docs/backlog.md`.
