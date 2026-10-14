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
dropped without being fetched and counted as pruned. Each ID is sent to the WIQL
filter at most once (a rejected ID to one more existence query, below), so WIQL work
is linear in the number of distinct queued IDs. If a WIQL chunk fails, it warns and its IDs are fetched and filtered after fetch instead; they are not
re-sent to WIQL. Accepted items reachable only through filtered items are not
exported in this mode.

WIQL returns only existing, readable items, so an ID the filter query rejects may be
missing rather than filtered. Rejected IDs are sent to one more WIQL query without
filters; only those it returns are pruned. The rest are fetched, so the batch fetch
reports them as omitted. If that existence query fails, all rejected IDs of the chunk
are fetched and filtered after fetch.

IDs requested but not returned by ADO (deleted or inaccessible) are counted as
omitted and are not retried. They are split the way `ado-triage` separates core items
from label lookups:

| Omitted | Status | Exit | Outputs |
| --- | --- | --- | --- |
| none | `complete` | 0 | written |
| only linked IDs (reached via relations) | `incomplete` | 4 | written |
| any seed ID (returned by the saved query) | `failed` | 5 | none written |

A missing linked ID loses a node and every path through it; whether it would have
been in scope or a bridge is unknown, so this is reported in both relation modes.
A missing seed means the requested scope itself is incomplete. Seeds head the queue,
so the run stops as soon as every seed is fetched or pruned. The last seed batch may
be topped up with linked IDs, so on a failed run the linked count is partial and
depends on the batch size; the seed count does not. The summary prints the omitted total, its seed and linked counts and the
completion status; the omitted IDs are logged sorted, up to 20 per list. Pruned items
are out of scope, not missing, and do not affect the status.

Recognized relations: hierarchy forward, affects forward/reverse, blocks
forward/reverse, dependency forward/reverse, implements forward/reverse. Hierarchy
reverse is present in the label map but commented out of the traversal allowlist.
Other links and URLs whose final component is not an integer are ignored.

### Exported data

Nodes contain `WI_ID`, every returned field and a boolean `In_Scope`. In `stop`
mode only in-scope items are exported (all `In_Scope=True`); an out-of-scope item
fetched after a WIQL failure is dropped, so the export is the same whether or not
the WIQL filter succeeded. In `follow` mode every fetched work item is a node, and
items excluded by type or project have `In_Scope=False` and act as bridge nodes for
analysis. Three names are changed:
`System.Title` → `Title`, `System.WorkItemType` → `WorkItemType`, `System.State` →
`State`. All other ADO field reference names remain unchanged. No source snapshot,
root marker, completeness manifest or export-schema version is added.

Edges contain `Source_ID`, `Target_ID`, `Link_Type`; direction is the emitting
item to its relation target. Rows are deduplicated by all three columns. Every
exported node contributes its edges; targets may be absent from the node file (not
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
comma-separated list). A step may name its subgraph as `step@subgraph`, for example
`cycles@dependency`; its sheet is then named `step@subgraph`, lowercased with
spaces removed, so one run can compare subgraphs. `all` in the list adds every
step on its default subgraph (`all,cycles@dependency`). Empty entries are ignored;
unknown steps and unknown subgraphs warn and are skipped. `--bridges` (`contract`
by default, or `drop`) and `--max-indirect-edges` (default 1,000,000) control bridge
nodes, below.

Minimum input columns: nodes `WI_ID`; edges `Source_ID`, `Target_ID`, `Link_Type`.
Optional node metadata: Title, WorkItemType, State, `KL.Roadmap_State`,
`System.AreaPath`, `System.IterationPath`, configured effort field. Schema/ID
validation is not performed; missing required columns raise exceptions.

Edges are normalized first. Reverse labels flip direction and normalize to their
forward label; forward directions are parent→child, affects→affected,
blocks→blocked, predecessor→successor, implements→implemented. Edges whose either
endpoint is absent from the node file are dropped; unknown labels are dropped with
a warning per label. Each forward label belongs to one relation family:

| Family | Forward labels | Direction means |
| --- | --- | --- |
| `hierarchy` | `Parent->Child` | the source contains the target |
| `dependency` | `Blocks`, `Predecessor->Successor` | the target waits for the source |
| `impact` | `Affects`, `Implements` | a change to, or work on, the source reaches the target |

Every normalized relation is kept in a `MultiDiGraph` keyed by its label, so the
same relation reported from both ends is one edge and different relation types on
the same pair stay separate edges; no label is overwritten. Each step runs on a
`DiGraph` projection of one named subgraph: `all` (every family), `hierarchy`,
`dependency` or `impact`. A projection has one edge per ordered pair; when several
relation types join that pair, the edge keeps all of them in `link_types` and joins
them in report order in `link_type`, for example `Parent->Child & Blocks`.

| Step | Default subgraph | Direction |
| --- | --- | --- |
| `cycles`, `scc`, `longest_chain`, `metrics`, `orphans` | `all` | as stored |
| `has_cr_dep`, `no_cr_dep` | `all` | downstream of Change Requests |
| `cr_scope_impact`, `cr_impact_unpacked` | `all` | downstream of each CR |
| `cr_prereqs`, `cr_prereqs_unpacked` | `dependency` | upstream of each CR |

So for a CR, its children, the items it blocks or precedes and the items it affects
are its impact; only the items that block or precede it are its prerequisites. Its
parent is neither, and appears upstream only with `cr_prereqs@all`. Items that
implement or affect a CR point at it, so they are upstream too: no default CR sheet
lists them, `no_cr_dep` does, and `cr_prereqs@impact` or `cr_prereqs@all` shows
them. On `all`, a cycle may mix families (a child that blocks its parent) and need
not be a planning error; `cycles@dependency` reports dependency cycles only.

When the node file has an `In_Scope` column, each out-of-scope bridge node B is
contracted in every projection, following only that subgraph's relations: for every
P → B → S it adds P → S with `link_type` `Indirect`, the bridge IDs in `via` and the
normalized link types along the path in `path`, unless a direct P → S edge already
exists (a direct link wins). Chains of bridges collapse into one edge, and when
several bridge routes join the same pair only the first one contracted is kept, so
another route's link types show only in a subgraph that lacks the first route; a
path that returns to its own start through bridges only is dropped rather than kept
as a self-loop. Bridge nodes are then removed, so no report lists them and CR effort
sums ignore them. The neighbor debug column always describes the `all` projection
and shows indirect edges as `Indirect via 2>3 (Parent->Child, Blocks)`. Node files
without `In_Scope` (older exports) are treated as fully in scope. This runs while
the analyzer is built, before any step and before output is created, so it is paid
even for `--steps orphans`. Each bridge costs in-degree × out-degree candidate edges
(a bridge with 5,000 links on each side would cost 25 million). The running total is
checked before each bridge is contracted; above `--max-indirect-edges` the command
exits with an error naming the bridge, and no report file or directory is created.
`--bridges drop` removes bridges and their edges without adding indirect edges,
which matches `stop` export semantics. The other projections are built on first use;
none can exceed the budget the `all` projection passed, because a subset of
relations never gives a bridge more neighbors. The budget applies to each
projection, so a run that uses all four may do up to four times that contraction
work. An indirect edge's `path` names each hop's link types, for example
`(Parent->Child & Blocks, Blocks)`. This is a structural graph, not a validated
scheduling graph.

| Step / sheet | Current meaning |
| --- | --- |
| `cycles` | Enumerates all simple cycles; one row per cycle member with metadata |
| `orphans` | Nodes with zero incoming and outgoing degree; full node fields plus URL/debug neighbors |
| `no_cr_dep` | Nodes that are neither a Change Request nor downstream of one |
| `has_cr_dep` | Change Requests and every node downstream of one (items that depend on a CR) |
| `longest_chain` | Longest path in SCC-condensed DAG, unweighted hop count in `Chain_Length`; every SCC member emitted |
| `scc` | Every strongly connected component including singletons; index, size, members, metadata |
| `metrics` | Degrees (distinct neighbors), exact normalized betweenness, longest-depth `Dependency_Depth` and metadata |
| `cr_scope_impact` | CR descendants, count/IDs; numeric effort of CR plus descendants, invalid effort becomes zero |
| `cr_prereqs` | CR ancestors (dependency subgraph by default), count/IDs and metadata |
| `cr_impact_unpacked` | One row per CR/downstream node pair with both metadata sets |
| `cr_prereqs_unpacked` | One row per CR/upstream node pair with both metadata sets |

CR recognition is exact `WorkItemType == "Change Request"`. `Dependency_Depth` is
the longest depth: the number of hops on the longest path from any source to the
item in the step's SCC-condensed DAG, computed by dynamic programming in
topological order (a source is 0; every other node is one more than its deepest
predecessor). A shortcut link therefore never lowers a depth: with A→B→C and
A→C, C has depth 2, although its shortest distance from A is 1. Members of one
strongly connected component share its depth, so a cycle counts as a single step.
The largest `Dependency_Depth` equals the `longest_chain` length on the same
subgraph. Releases before AG-007 reported the maximum shortest distance from a
source instead, which could be lower.

The `longest_chain` sheet repeats the chain's hop count in a `Chain_Length` column
on every row (the DataFrame attr `longest_path_length` and the log carry the same
value). Set iteration and ties may change row order and which of several equally
long chains is listed; deterministic report ordering is not guaranteed.

Both measures count links (hops), not time. The graph has no duration, effort-based
scheduling, capacity or calendar model, so the longest chain is the longest
sequence of linked items, not a critical path: a chain of three one-day tasks has
more hops than one three-month epic but finishes sooner. A critical path would need
per-item durations and edges that mean finish-to-start, which hierarchy and impact
links do not.

Analysis writes requested sheets, no index, with widths capped at 50. `all` runs
every step, including unbounded simple-cycle enumeration and exact centrality.
No budgets, sampling, cancellation or progress policy are provided for analysis.
Width sizing walks every written cell in Python. Unpacked/cycle outputs have no
preflight check against Excel's 1,048,576-row limit (including the header), so a
large result may fail after all graph computation and leave a partial workbook.

## Files, errors and current limitations

Both modes publish atomically. Export writes every file of the requested formats
(`nodes.parquet`/`edges.parquet`, `nodes.csv`/`edges.csv`, `work_items.xlsx`) to
hidden sibling temp files and renames the whole bundle into place only after every
write succeeded; analyze does the same for its single workbook. A failed write
(permissions, full disk, serialization error) leaves every existing output
unchanged and removes the temp files. Unknown and repeated formats are skipped.

An existing output needs `--overwrite`. Without it the run exits with code 2 and
names the files before export calls ADO or analyze reads its inputs. Analyze
refuses an `--output-file` that is the `--nodes-file` or `--edges-file` (also
through a link) with exit 2, even with `--overwrite`. Consent is checked when the
run starts: a file created at an output path during a long export is replaced.
Export still creates `--output-dir` before fetching. Empty exports do not preserve a stable node schema; all-unknown steps can fail
when saving a workbook with no sheets. Exceptions normally propagate. A complete
run exits 0; export can also exit with the partial-result statuses described under
[Traversal, filtering and completeness](#traversal-filtering-and-completeness).
Argparse errors/help use its usual exits (2 for invalid options, existing outputs
and input collisions). Text beginning with `=` can be
written as an Excel formula by pandas/openpyxl; arbitrary identity/datetime field
objects may fail serialization.

## Verification and next steps

Tests use synthetic SDK-like objects and Parquet/XLSX files: CLI help and argument
validation, saved-query result shapes, edge normalization, scope/prerequisite
orientation, report publication, traversal through filtered bridges in both
relation modes, once-per-ID WIQL classification, omitted-ID reporting, and bridge
contraction. No actual ADO server was used. Remaining fixes and shared-code
opportunities with ADO scope triage are in [the backlog](../backlog/README.md#ado-graph-and-triage-sharing) (AG and SH tasks).
