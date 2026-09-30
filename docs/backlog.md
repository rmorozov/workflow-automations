# Backlog

These are proposals, not promises made by the current specifications.

## XLSX translation

- Reuse a source bundle for multiple target languages without repeating extraction.
- Reuse reviewed translations across bundles with explicit context and language keys.
- Export pending-only LLM batches from completed replies while retaining bundle IDs.
- Optional row-context keys for identical text with different meanings within one column.
- JSON configuration for repeatable selections, languages and batch limits.
- Model-specific token budgeting and placeholder/markup validation.
- Source CSV table support.
- Measured large-workbook performance improvements.
- Workbook fidelity and formula-reference rewriting, with clearly defined supported features.

## Repository workflow

- Enable branch protection/rulesets and require the shared CI checks when ready.
- Add scheduled integrations only when a tool needs them; keep credentials outside the repository.
