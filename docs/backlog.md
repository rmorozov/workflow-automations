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

## XLSX outline

- Optional retyping of unfolded and merged numbers and dates.
- Adjust Excel tables, data validation and conditional formatting ranges on merge.
- Row tags in a form that more mind-map editors keep, if HTML comments get dropped.

- Outline every sheet of a workbook, one heading per sheet.
- Tables that do not start at A1 or have no heading row.
- Detail columns as a Markdown table under each leaf instead of one line per row.
- CSV input.

## ADO scope triage

- Per-team output (one small workbook or sheet per team lead) to route actions to owners.
- Write triage decisions back to ADO (tags or comments); read-only until then.
- Trend history across more than one previous report (team load and score over time).
- Transitive blocker chains (critical path) instead of one-hop blocker counts.
- Planned-vs-capacity view based on `OriginalEstimate`, alongside remaining work.
- Embedded summary chart (remaining work vs capacity per team).
- Merge sibling sheets (estimation debt by date/hours; estimate-quality sheets).
- Configurable field names, hour thresholds, flag weights and RAG bands (JSON config).
- Offline replay: save the fetched snapshot as JSON and rebuild reports from it.
- Restrict fetched fields to those used, to reduce payload on very large queries.

## Repository workflow

- Enable branch protection/rulesets and require the shared CI checks when ready.
- Add scheduled integrations only when a tool needs them; keep credentials outside the repository.
