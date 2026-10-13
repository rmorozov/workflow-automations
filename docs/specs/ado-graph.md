# ADO graph/export specification

Status: started as a verbatim import of the user's script (PR #11) and is now being
reworked under normal checks. Sections below describe current behavior; known
limitations not yet addressed stay listed here and in the backlog.

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
| `--batch-size` | Default 200; upper-clamped to 200; values below 1 are rejected before any service call |
| `--cert-dir` | Corporate certificate directory; falls back to `ADO_CERT_DIR` |
| `--output-dir` | Default `./output`; directory created |
| `--formats` | Comma-separated `parquet,excel,csv` by default; unknown names logged/skipped |
| `--skip-types` | Case-insensitive comma list of types marked out of scope; empty string disables |
| `--include-projects` | Case-insensitive comma list of projects kept in scope; empty means all |
| `--filtered-relations` | `stop` (default) or `follow`; whether relations of filtered items are traversed |

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
Each batch requests relations with `error_policy="Omit"`. Project is
`System.TeamProject`, falling back to the first AreaPath segment. Filters decide an
item's output scope (`In_Scope`), not whether it is reachable.

With `--filtered-relations follow` every fetched item's recognized relation
targets are queued, whether or not the item is in scope. No WIQL pre-filtering
happens, because a WIQL filter cannot see relations: an accepted item reachable only
through a Task, Bug or excluded-project item is still found. This fetches every
reachable filtered item, so it costs more ADO requests than `stop`. Excluded-project
items are followed too, so `--include-projects` limits output, not the walk: links
from a query's items can pull in dozens of unrelated projects.

With `--filtered-relations stop` (default), relations of out-of-scope items are not
followed.
Before a fetch batch that contains IDs not yet checked, up to 1,000 unchecked queued
IDs from the head of the queue are sent to one WIQL filter query; IDs it rejects are
dropped without being fetched and counted as pruned. Each ID is sent to WIQL at most
once, so WIQL work is linear in the number of distinct queued IDs. If a WIQL chunk
fails, it warns and its IDs are fetched and filtered after fetch instead; they are not
re-sent to WIQL. Accepted items reachable only through filtered items are not
exported in this mode.

IDs requested but not returned by ADO (deleted or inaccessible) are counted as
omitted and logged with a warning listing up to 20 IDs. They are not retried, and the
run still exits 0; there is no separate incomplete-run status yet.

Recognized relations: hierarchy forward, affects forward/reverse, blocks
forward/reverse, dependency forward/reverse, implements forward/reverse. Hierarchy
reverse is present in the label map but commented out of the traversal allowlist.
Other links and URLs whose final component is not an integer are ignored.

### Exported data

Nodes contain `WI_ID`, every returned field and a boolean `In_Scope`. Every fetched
work item is a node: in-scope items have `In_Scope=True`; items excluded by type or
project have `In_Scope=False` and act as bridge nodes for analysis. Three names are changed:
`System.Title` → `Title`, `System.WorkItemType` → `WorkItemType`, `System.State` →
`State`. All other ADO field reference names remain unchanged. No source snapshot,
root marker, completeness manifest or export-schema version is added.

Edges contain `Source_ID`, `Target_ID`, `Link_Type`; direction is the emitting
item to its relation target. Rows are deduplicated by all three columns. Every
fetched item contributes its edges; targets may be absent from the node file (not
fetched, pruned, or omitted).

| Format | Files/sheets |
| --- | --- |
| Parquet | `nodes.parquet`, `edges.parquet` |
| CSV | `nodes.csv`, `edges.csv`; pandas defaults, header and no index |
| Excel | `work_items.xlsx`, sheets `Nodes`, `Edges`; pandas/openpyxl defaults |

Batch progress and per-type/project statistics are printed, followed by the pruned
(WIQL-rejected, not fetched) and omitted (not returned by ADO) totals. Per-type and
per-project totals count fetched items only.

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

The analyzer uses a `DiGraph`, adds every exported node, and drops edges whose
either endpoint is absent. When the node file has an `In_Scope` column, each
out-of-scope bridge node B is then contracted: for every P → B → S it adds
P → S with `link_type` `Indirect`, the bridge IDs in `via` and the normalized link
types along the path in `path`, unless a direct P → S edge already exists (a direct
link wins). Chains of bridges collapse into one edge; a path that returns to its
own start through bridges only is dropped rather than kept as a self-loop. Bridge
nodes are then removed, so no report lists them and CR effort sums ignore them. The
neighbor debug column shows indirect edges as `Indirect via 2>3 (Parent->Child, Blocks)`.
Node files without `In_Scope` (older exports) are treated as fully in scope. Reverse labels flip direction and normalize to their
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
Width sizing walks every written cell in Python. Unpacked/cycle outputs have no
preflight check against Excel's 1,048,576-row limit (including the header), so a
large result may fail after all graph computation and leave a partial workbook.

## Files, errors and current limitations

Outputs are written directly and overwrite existing files without an overwrite
flag, input-path protection, staging or rollback. Failed runs may leave partial
files. This inherited behavior differs from the other tools' publication contract
and has not been reworked yet. Work in a fresh output directory. Empty exports do not preserve a stable node schema; all-unknown steps can fail
when saving a workbook with no sheets. Exceptions normally propagate; successful
`main()` returns None (CLI exit 0), argparse errors/help use its usual exits.
There is no standardized partial-result status. Text beginning with `=` can be
written as an Excel formula by pandas/openpyxl; arbitrary identity/datetime field
objects may fail serialization.

## Verification and next steps

Tests use synthetic SDK-like objects and Parquet/XLSX files: CLI help and argument
validation, saved-query result shapes, edge normalization, scope/prerequisite
orientation, report publication, traversal through filtered bridges in both
relation modes, once-per-ID WIQL classification, omitted-ID reporting, and bridge
contraction. No actual ADO server was used. Remaining fixes and shared-code
opportunities with ADO scope triage are in [the backlog](../backlog.md#ado-graphexport).
