# ADO graph/export specification — preserved import

Status: observed contract restored from the supplied `Pasted text.txt`, not a
redesign. `src/workflow_automations/ado_graph.py` preserves all 1,477 lines byte for
byte (SHA-256 `3dba9aef1314d52c73510cdd72c976a47e2c02312e09706591812cd93372c015`).
The original module docstring describes an older pre-seed implementation; where it
conflicts with executable code, the behavior below takes precedence.

Entry points after installation: `ado-graph`, `python scripts/ado_graph.py`, and
`python -m workflow_automations.ado_graph`. Argument help retains `prog=ado_tool`.
Install the `ado-graph` extra. All dependencies, including Kerberos, are imported
at module startup, even for `--help` and offline `analyze`. Linux installations may
need system Kerberos headers (`libkrb5-dev`) to build dependencies.

## Purpose and boundaries

`export` reads an on-premise ADO collection through its SDK, starts with a saved
query, traverses selected work-item relations, and writes node/edge tables.
`analyze` reads local Parquet tables, builds a NetworkX graph, and writes Excel
analysis sheets. Neither mode modifies ADO. Analysis makes no service calls.
The tool is independent of `ado-triage`; it does not compute capacity, attention
scores, RAG bands, or triage decisions.

## Export CLI

| Option | Current behavior/default |
| --- | --- |
| `--ado-url` | Required collection URL; trailing slash removed by client |
| `--query-id` | Required saved-query GUID; flat results or source/target IDs from links |
| `--project` | Query team context; default empty |
| `--batch-size` | Default 200; upper-clamped to 200, no positive-value validation |
| `--cert-dir` | Corporate certificate directory; falls back to `ADO_CERT_DIR` |
| `--output-dir` | Default `./output`; directory created |
| `--formats` | Comma-separated `parquet,excel,csv` by default; unknown names logged/skipped |
| `--skip-types` | Case-insensitive comma list; empty string disables filtering |
| `--include-projects` | Case-insensitive comma list; empty means all projects |

Default excluded types: Task, Bug, Test Case, Test Suite, Test Plan, Test Run,
Shared Steps, Shared Parameter.

A temporary PEM combines certifi's bundle and recursively found `.pem`/`.crt`
files (sorted separately per extension). It replaces `REQUESTS_CA_BUNDLE` for the
process. Invalid certificate directories warn and use certifi alone. The temporary
file is not deleted by this script. Authentication uses msrest
`KerberosAuthentication(mutual_authentication=OPTIONAL)` with the current ticket
cache. No password/PAT options or retry/timeout policy are exposed.

### Traversal, filtering and completeness

The saved query seeds an insertion-ordered queue; visited IDs avoid repeated fetches.
Each batch requests relations with `error_policy="Omit"`. Omitted IDs are not retried
or reported individually. Returned work items enqueue recognized relation targets
before output filtering, so fetched excluded items can normally bridge to accepted
items. Project is `System.TeamProject`, falling back to the first AreaPath segment.

When filters are active and the queue contains at least three batches, the next
batch is retained and the remaining IDs are filtered in WIQL chunks of 1,000. IDs
rejected by that query are marked visited without being fetched or traversed.
Consequently this optimization can lose accepted descendants reachable only through
filtered items. The help text's unconditional traversal promise is not true in this
case. Failed WIQL chunks warn and retain candidates for normal fetch/filtering.

`WIIndex` and its project-wide keyset/pre-seed routines remain in the source but
are not used by `run_export`. No up-front whole-project pre-seed occurs.

Recognized relations: hierarchy forward, affects forward/reverse, blocks
forward/reverse, dependency forward/reverse, implements forward/reverse. Hierarchy
reverse is present in the label map but commented out of the traversal allowlist.
Other links and URLs whose final component is not an integer are ignored.

### Exported data

Nodes contain `WI_ID` and every returned field. Three names are changed:
`System.Title` → `Title`, `System.WorkItemType` → `WorkItemType`, `System.State` →
`State`. All other ADO field reference names remain unchanged. No source snapshot,
root marker, completeness manifest or export-schema version is added.

Edges contain `Source_ID`, `Target_ID`, `Link_Type`; direction is the emitting
item to its relation target. Rows are deduplicated by all three columns. Only
accepted emitting items contribute edges; targets may be absent from the node file.

| Format | Files/sheets |
| --- | --- |
| Parquet | `nodes.parquet`, `edges.parquet` |
| CSV | `nodes.csv`, `edges.csv`; pandas defaults, header and no index |
| Excel | `work_items.xlsx`, sheets `Nodes`, `Edges`; pandas/openpyxl defaults |

Batch progress and per-type/project statistics are printed. WIQL-pruned or omitted
IDs are not included in the per-type/project totals, so those totals are not a
complete account of every encountered ID.

## Analyze CLI and graph contract

Required: `--nodes-file`, `--edges-file` (Parquet only; CSV/Excel exports cannot be
loaded by this mode). Optional: `--ado-url` (default empty, for report URLs),
`--effort-field` (default `Microsoft.VSTS.Scheduling.OriginalEstimate`),
`--output-file` (default `analysis_report.xlsx`), `--steps` (default `all`, or a
comma-separated list). Unknown steps warn and are skipped.

Minimum input columns: nodes `WI_ID`; edges `Source_ID`, `Target_ID`, `Link_Type`.
Optional node metadata: Title, WorkItemType, State, `KL.Roadmap_State`,
`System.AreaPath`, `System.IterationPath`, configured effort field. Schema/ID
validation is not performed; missing required columns raise exceptions.

The analyzer uses a `DiGraph`, includes every exported node, and drops edges whose
either endpoint is absent. Reverse labels flip direction and normalize to their
forward label. Forward directions are parent→child, affects→affected, blocks→blocked,
predecessor→successor, implements→implemented. Parallel relation types on the same
pair collapse to one edge; the last label wins. All relation families participate
in every analysis. This is a structural graph, not a validated scheduling graph.

| Step / sheet | Current meaning |
| --- | --- |
| `cycles` | Enumerates all simple cycles; one row per cycle member with metadata |
| `orphans` | Nodes with zero incoming and outgoing degree; full node fields plus URL/debug neighbors |
| `no_cr_dep` | Nodes outside the reverse-reachable set of Change Requests |
| `has_cr_dep` | Change Requests and their ancestors (reverse reachability), not their descendants |
| `longest_chain` | Longest path in SCC-condensed DAG, unweighted hop count; every SCC member emitted |
| `scc` | Every strongly connected component including singletons; index, size, members, metadata |
| `metrics` | Degrees, exact normalized betweenness, Dependency_Depth and metadata |
| `cr_scope_impact` | CR descendants, count/IDs; numeric effort of CR plus descendants, invalid effort becomes zero |
| `cr_prereqs` | CR ancestors, count/IDs and metadata |
| `cr_impact_unpacked` | One row per CR/downstream node pair with both metadata sets |
| `cr_prereqs_unpacked` | One row per CR/upstream node pair with both metadata sets |

CR recognition is exact `WorkItemType == "Change Request"`. `Dependency_Depth` is
the maximum, over source SCCs, of each source's *shortest* distance to an SCC; it
is not longest prerequisite-chain depth. Longest-chain length is stored in
DataFrame attrs and logs, not as a dedicated persisted Excel value. Set iteration
and ties may change row order; deterministic report ordering is not guaranteed.
The graph has no duration/capacity model, so longest chain is not a time critical path.

Analysis writes requested sheets, no index, with widths capped at 50. `all` runs
every step, including unbounded simple-cycle enumeration and exact centrality.
No budgets, sampling, cancellation or progress policy are provided for analysis.

## Files, errors and current limitations

Outputs are written directly and overwrite existing files without an overwrite
flag, input-path protection, staging or rollback. Failed runs may leave partial
files. This inherited behavior differs from the other tools' publication contract
and is retained only for this verbatim import. Work in a fresh output directory.
Empty exports do not preserve a stable node schema; all-unknown steps can fail
when saving a workbook with no sheets. Exceptions normally propagate; successful
`main()` returns None (CLI exit 0), argparse errors/help use its usual exits.
There is no standardized partial-result status. Text beginning with `=` can be
written as an Excel formula by pandas/openpyxl; arbitrary identity/datetime field
objects may fail serialization. No actual ADO server was used in offline tests.

## Verification and next steps

Tests use synthetic SDK-like objects and Parquet/XLSX files: import integrity,
CLI help, saved-query result shapes, edge normalization, scope/prerequisite
orientation, report publication, and the filtered-bridge regression. Known defects
are characterized, not silently corrected. Prioritized fixes and shared-code
opportunities with ADO scope triage are in [the backlog](../backlog.md#ado-graphexport).
