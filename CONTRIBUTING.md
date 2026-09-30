# Contributing

Use small branches and pull requests. A change should be understandable from its
specification, command help, example, and tests without prior conversation context.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/workflow_automations/` | Importable tools with CLI entry points |
| `scripts/` | Thin Python-script entry points where useful |
| `docs/specs/<tool>.md` | Current implemented behavior and explicit limitations |
| `tests/<tool>/` | Synthetic fixtures and meaningful behavior tests |
| `docs/backlog.md` | Proposed extensions, separate from implemented requirements |
| `.github/workflows/` | Shared CI |

Keep each tool independent. Introduce shared abstractions when a second tool needs
them. Put tool-specific dependencies in optional extras rather than forcing them
on every user. Keep code and specification changes in the same PR.

## Local checks

```bash
python -m pip install -e '.[translation,dev]'
ruff check .
ruff format --check .
pytest
python -m build
```

Use `ruff format .` to format changes. Tests generate their workbooks in temporary
directories and must not require accounts, corporate services, an LLM, or network.
Use only synthetic data in fixtures, examples, issues, and PRs. Generated workbooks,
translation bundles and reports belong under ignored `work/`, outside the repository,
or in temporary directories.

CI runs lint/format and tests on supported Python/dependency combinations, builds a
wheel, installs that wheel, and smoke-tests both CLI entry points. Workflow tokens
have read-only contents permission. Actions are pinned to commit SHAs and dependency
updates are proposed by Dependabot.

Do not merge a failing PR. Repository branch protection is an owner setting; the
workflow does not configure it. Recommended required checks are `quality` and all
`test` matrix jobs; update those names if the matrix changes.

## Adding a tool

Add its specification, module/entry point, dependency extra when needed, tests and a
README table entry. CI installs the union of extras required by the test suite; add
new extras to the installation commands. Describe input/output, overwrite behavior,
exit codes, offline/network behavior and limits in the specification. Avoid adding
a scheduler or external integration until the tool actually needs one.
