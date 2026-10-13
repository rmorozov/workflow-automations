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
- Faster source reading than openpyxl's read-only parser (now the dominant cost when
  a source must be re-verified and during extraction).
- Workbook fidelity and formula-reference rewriting, with clearly defined supported features.

## XLSX outline

- Optional retyping of unfolded and merged numbers and dates.
- Adjust Excel tables, data validation and conditional formatting ranges on merge.
- Row tags in a form that more mind-map editors keep, if HTML comments get dropped.

- Outline every sheet of a workbook, one heading per sheet.
- Tables that do not start at A1 or have no heading row.
- Detail columns as a Markdown table under each leaf instead of one line per row.
- CSV input.

## Gerrit parents

- Match by Gerrit path (`platform/tools/repo`) as well as by the last path segment.
- Read the tree directly from Gerrit (`gerrit ls-projects --tree` or the REST API).
- Add the Parent column into a copy of the original workbook, keeping types and formatting.

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

## ADO graph/export

Findings from the verbatim import and comparison with `ado-triage`. The current
specification records these limitations; the items below are future work. AG-001,
AG-014 and AG-015 (traversal through filtered bridges, once-per-ID WIQL filtering,
bridge nodes kept connected in analysis) are implemented.

| Priority / ID | Finding and concrete follow-up | Acceptance evidence |
| --- | --- | --- |
| P1 AG-002 | Add atomic publication, explicit overwrite and input/output collision checks to both modes; multi-file export needs a staged bundle. | Failed writes leave existing files intact; analysis cannot overwrite either input; overwrite requires consent via flag. |
| P1 AG-003 | Omitted IDs are now counted and logged; still missing: a completion status/exit code instead of treating `error_policy=Omit` as a complete graph. Use triage's core-vs-label failure distinction. | Missing seed/bridge IDs yield explicit incomplete/failure status and reproducible counts. |
| P1 AG-004 | Validate graph semantics: keep relation types separate; preserve parallel links; choose hierarchy, dependency and impact subgraphs explicitly. Reverse CR reachability currently means ancestors despite the “CR dependency” name. | Synthetic CR→child, predecessor→CR and multiple-type edges have documented, distinct outputs; no labels silently overwritten. |
| P1 AG-005 | Store exported titles/text as literal Excel cells, normalize identity objects and timezone-aware dates, and validate schemas/IDs/empty frames before writing. | Formula-like titles stay strings; realistic identity/date fields and an empty export round-trip through all formats. |
| P2 AG-006 | Bound cycle enumeration and centrality cost; add step budgets, approximate centrality and cancellation/progress. Default all can be exponential on dense cycles. | Representative 10k/50k graphs finish under stated budgets or report a bounded partial result. |
| P2 AG-007 | Correct Dependency_Depth to a named metric (longest DAG depth via topological DP, or rename maximum source-shortest distance); persist chain length and explain hop count vs time critical path. | A shortcut A→C plus A→B→C distinguishes shortest distance 1 from longest depth 2; report exposes the selected metric. |
| P2 AG-008 | Harden WIQL quoting and keyset pagination. Project builder uses backslash quoting while filter builder doubles quotes; skipped-type names are interpolated unescaped; ORDER regex can match string literals. | Apostrophes and ORDER inside names, paging >20k IDs, and nonadvancing pages are covered using SDK fakes and an optional real-server contract test. |
| P2 AG-009 | Validate supported formats/steps and file schemas early (batch size is now validated). All-unknown steps lead to an empty-workbook save failure. | Invalid input exits predictably before service calls or file creation. |
| P2 AG-010 | Make local analysis installable without Azure SDK/Kerberos; move imports/client configuration to a network boundary. Remove import-time global logging and temporary CA/environment side effects; clean up and restore prior CA settings. | Offline analyze/help work with no network extras; importing a library does not configure root logging; CA cleanup works on failure. |
| P2 AG-011 | Unused WIIndex code is removed and pruned/omitted counts are recorded. Still missing: measured request counts in documentation. | Counters reconcile seed, visited, fetched, pruned, omitted and exported IDs; documentation matches requests observed in tests. |
| P2 AG-012 | Define a versioned snapshot schema and provenance manifest (query, collection, fields, filters, relation policy, timestamps, tool version, completeness); add replay and CSV/XLSX ingestion if needed. | Old/new snapshots validate; identical snapshots can feed both graph and triage pipelines offline. |
| P3 AG-013 | Sort node/SCC/cycle/CR member outputs deterministically; add report purpose/config sheets, hyperlinks, filters and concise summary counts before detailed/unpacked sheets. | Repeat runs on the same snapshot compare stably and expose scope/completeness in the report itself. |

### Additional findings from PR #11 review

| Priority / ID | Finding and concrete follow-up | Acceptance evidence |
| --- | --- | --- |
| P2 AG-016 | Preflight Excel's row/column/cell limits before publication; CR×reachable and cycle-member sheets can exceed 1,048,575 data rows plus header. Spill large detail sheets to Parquet/CSV with pointers in the workbook, or fail before any output is replaced. Replace the full openpyxl cell walk for widths with bounded sampling; vectorized full-column sizing remains O(cells). Remove the bare except and report sizing failures explicitly. | Boundary tests for supported counts and spill behavior; existing outputs survive overflow; large-report benchmarks separate graph compute, writing and sizing time. |
| P3 AG-017 | Make custom metadata fields configurable. `KL.Roadmap_State` is currently embedded in graph metadata despite being organization-specific. Add explicit field mapping/extra-fields configuration, keeping existing names as an opt-in compatibility preset. | Synthetic custom fields appear under configured labels; absent fields are reported/omitted according to policy; defaults need no organization-specific field. |

Next: AG-010 (offline dependency split), then AG-002/AG-005/AG-016 (safe export), and
the remaining relation/depth semantics (AG-004, AG-007).

### Sharing code with ADO scope triage

Both tools use the Azure SDK, Kerberos OPTIONAL, relation URLs, batches, field
metadata, pandas and Excel. Share small tested boundaries rather than merging the
CLI modes or their different notions of scope. No production refactor is made in
this import.

| Candidate | Current difference and proposed boundary | Sequence |
| --- | --- | --- |
| ADO session/auth/CA helpers | Graph uses msrest's adapter and global merged CA; triage defines a local requests-kerberos adapter and supports ambient CA settings. Extract configurable session/auth plus scoped CA lifetime; retain per-tool errors and mutual-auth behavior. | After auth integration tests and AG-010 |
| Saved-query ID extraction and batch helpers | Both handle flat/link results and cap batches at 200. Graph uses batch API/Omit and BFS; triage uses strict core fetch plus optional external metadata. Share pure ID extraction/chunking and transport result types, not omission policies. | First low-risk extraction after characterization |
| Relation parsing and direction registry | Graph uses emitting-item Target_ID and forward labels; triage uses Destination_ID plus item-relative labels. Share raw reference names and endpoint parsing, with explicit adapters to each schema. Hierarchy-Reverse must remain an explicit policy. | Alongside AG-004 |
| Snapshot/data normalization | Triage already has plain Snapshot and build_report; graph retains SDK objects until DataFrames and loses root/provenance information. Add a versioned raw snapshot and adapters to each tool; do not use graph-filtered nodes as a complete triage scope. | AG-012 after completeness fixes |
| Work-item URLs and scalar conversions | Duplicate small pure helpers exist. Centralize them with boundary tests without pulling graph dependencies into triage. | Early, only where both contracts match |
| Excel text/date safety and atomic publication | Triage has _excel_safe, write_excel and publish; graph writes directly. Promote tested shared IO primitives with explicit error/overwrite adapters, not a universal report-layout class. | AG-002 and AG-005 |
| Reachability/critical-path analysis | Triage currently counts one-hop blockers; graph can supply separate normalized dependency subgraphs. Keep triage weights, capacity matching, attention scores and RAG in triage. | After AG-004/AG-007; extend existing transitive-blocker backlog |

The highest-value order is completeness and safe IO → explicit graph semantics →
small transport/normalization helpers → shared snapshot replay → performance/UX.
Avoid a general-purpose ADO framework until both tools require the same contract.
