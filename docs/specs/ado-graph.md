# ADO graph/export specification

Status: started as a verbatim import of the user's script (PR #11) and is now being
reworked under normal checks. Sections below describe current behavior; known
limitations not yet addressed stay listed here and in the backlog.

Entry points after installation: `ado-graph`, `python scripts/ado_graph.py`, and
`python -m workflow_automations.ado_graph`. Argument help retains `prog=ado_tool`.
Install the `ado-graph` extra for export, or only `ado-graph-analyze` for offline
analysis (see below). Linux installations of `ado-graph` may need system Kerberos
headers (`libkrb5-dev`) to build dependencies.

## Purpose and boundaries

`export` reads an on-premise ADO collection through its SDK, starts with a saved
query, traverses selected work-item relations, and writes node/edge tables.
`analyze` reads local Parquet tables, builds a NetworkX graph, and writes Excel
analysis sheets. Neither mode modifies ADO. Analysis makes no service calls.
The tool is independent of `ado-triage`; it does not compute capacity, attention
scores, RAG bands, or triage decisions.

Analysis and `--help` need only the `ado-graph-analyze` extra (pandas, openpyxl,
NetworkX, PyArrow). The ADO SDK, msrest, Kerberos, certifi and tqdm are in the
`ado-graph` extra and load only when `export` runs; without them `export` exits 2
before any work, naming the extra. Importing the package configures neither root
logging nor certificates. Export sets `REQUESTS_CA_BUNDLE` to a merged temporary
bundle while it runs, then deletes the bundle and restores the previous setting,
also when export fails.

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
| `--save-snapshot` | Also save the fetched work items as a JSON snapshot for offline `replay`; see [Snapshots and offline replay](#snapshots-and-offline-replay) |

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

Project and type names enter WIQL only as string literals with each `'` doubled, so
apostrophes, `ORDER BY` or brackets in a name cannot change the query. Every WIQL ID
query is paged by ascending ID (`[System.Id] > <last> ORDER BY [System.Id] ASC`, up to
20,000 rows per page) until a page comes back short, so no result is cut at the
server limit. A page that returns an ID at or below the previous page's highest
raises instead of looping; for a filter query this is a failed chunk, handled as
above.

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
`State`. All other ADO field reference names remain unchanged. The tables carry no
root marker or schema version; a saved snapshot (below) records the run instead.

Before any file is written, the frames are validated and normalized so every format
stores the same values:

- Nodes need a unique, non-null integer `WI_ID` and unique column names; edges need
  exactly `Source_ID`, `Target_ID`, `Link_Type` with non-null integer IDs. A
  violation raises an error and leaves existing files unchanged.
- An empty export still has typed columns: nodes `WI_ID` (int64) and `In_Scope`
  (bool), edges `Source_ID`, `Target_ID` (int64) and `Link_Type` (text).
- Identity objects (`System.AssignedTo` and other fields with `displayName` or
  `uniqueName`) become `Display Name <unique name>`, or whichever part is present.
  Other objects and lists become JSON text with sorted keys.
- Timezone-aware datetimes, and columns whose values are all ISO-8601 timestamps
  with an offset (as the ADO REST API returns dates), become naive UTC datetimes,
  because Excel cannot store an offset.
- A column that mixes text with other values is written as text.

Edges contain `Source_ID`, `Target_ID`, `Link_Type`; direction is the emitting
item to its relation target. Rows are deduplicated by all three columns. Every
exported node contributes its edges; targets may be absent from the node file (not
fetched, pruned, or omitted).

| Format | Files/sheets |
| --- | --- |
| Parquet | `nodes.parquet`, `edges.parquet` |
| CSV | `nodes.csv`, `edges.csv`; pandas defaults, header and no index |
| Excel | `work_items.xlsx`, sheets `Nodes`, `Edges`; text starting with `=` stays a literal string cell, never a formula |

Batch progress and per-type/project statistics are printed, followed by the pruned
(WIQL-rejected, not fetched) and omitted (not returned by ADO) totals. Per-type and
per-project totals count fetched items only.

#### Snapshots and offline replay

`export --save-snapshot PATH` also writes every fetched work item, in or out of
scope, as a [version 1 ADO snapshot](ado-snapshot.md): every field as ADO returned
it and every relation, in fetch order. Its provenance is the run's
`source` (`base_url` collection URL, `project`, `query_id`), `captured_at` (UTC
time the saved query ran), the saved query's IDs as `roots`, the omitted IDs, the
case-normalised `skip_types` and `include_projects` filters, and in `options` the
`filtered_relations` policy, the producing `tool_version` and the run's
`completeness` (`complete`, or `incomplete` when linked IDs were omitted). Pruned
IDs are not fetched and are not in it. An existing snapshot file is refused before
fetching unless `--overwrite` is passed, as are a directory and a path that is one
of the export files (also through a link); as for the export files, a snapshot
created at the path during the export is replaced. The snapshot is written,
atomically, before the export files, so it is kept when writing the tables fails;
an export that exits 5 writes neither. When the snapshot cannot be written (an
unwritable path, a full disk, field values that are not JSON data) the error is
logged, the export files are still written, and export exits 6, before exit 4.

`ado-graph replay --snapshot PATH [--output-dir DIR] [--formats …] [--overwrite]`
writes the export files of an `ado-graph` snapshot without contacting ADO, needing
only the `ado-graph-analyze` extra. It applies the recorded filters and relation
policy to the recorded items, so a snapshot replays into the same files as its
export, and exits 4 when the snapshot's linked IDs were omitted, as the export did.
Completeness comes from `omitted`, not from the recorded option, so snapshots saved
without the provenance options replay the same. Output consent and atomic
publication are as for export. A file that is not a valid snapshot, an `ado-triage`
snapshot or one with an omitted root exits 2 and writes nothing. An unfiltered
snapshot (`--skip-types ''`, no `--include-projects`) is also a complete
`ado-triage` scope for the same saved query; a filtered one is not. CSV or Excel
exports are not read back: the snapshot is the replay input.

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
| `cycles` | Enumerates simple cycles within a budget; one row per cycle member with metadata and `Cycles_Complete` |
| `orphans` | Nodes with zero incoming and outgoing degree; full node fields plus URL/debug neighbors |
| `no_cr_dep` | Nodes that are neither a Change Request nor downstream of one |
| `has_cr_dep` | Change Requests and every node downstream of one (items that depend on a CR) |
| `longest_chain` | Longest path in SCC-condensed DAG, unweighted hop count in `Chain_Length`; every SCC member emitted |
| `scc` | Every strongly connected component including singletons; index, size, members, metadata |
| `metrics` | Degrees (distinct neighbors), normalized betweenness (exact or sampled, `Betweenness_Sources`), longest-depth `Dependency_Depth` and metadata |
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
value).

Output order is deterministic and independent of the row order of the input files:
the analyzer sorts nodes by `WI_ID` and edges by source, target and label before
building the graph, so bridges are contracted in ID order and every traversal is
repeatable. Within the sheets:

| Sheet | Row order |
| --- | --- |
| `cycles` | Each cycle starts at its smallest member and keeps its direction; cycles shortest first, then by member sequence; `Cycle_Index` follows that order |
| `scc` | Largest component first, ties by smallest member; members ascending; `SCC_Index` follows that order |
| `longest_chain` | Chain order; among equally long chains, the one met first when components are visited by their smallest member; members of a component ascending |
| `orphans`, `no_cr_dep`, `has_cr_dep`, `metrics` | `WI_ID` ascending |
| `cr_scope_impact`, `cr_prereqs` | Effort total or count descending, ties by `CR_ID`; the ID lists ascending |
| `cr_impact_unpacked`, `cr_prereqs_unpacked` | `CR_ID`, then the related item's ID |
| neighbor debug column | Predecessors, then successors, each by ID |

`longest_chain` keeps `SCC_Index` from the condensation of its own step, which is
not the `scc` sheet's index. When `cycles` stops at its budget, the cycles listed are
the first ones networkx enumerates on that graph, so the same snapshot and settings
list the same cycles.

Both measures count links (hops), not time. The graph has no duration, effort-based
scheduling, capacity or calendar model, so the longest chain is the longest
sequence of linked items, not a critical path: a chain of three one-day tasks has
more hops than one three-month epic but finishes sooner. A critical path would need
per-item durations and edges that mean finish-to-start, which hierarchy and impact
links do not.

Two steps can cost far more than the graph's size, so each has a budget.
Simple cycles can number exponentially many in a dense strongly connected
component (a complete graph of 12 items has over 10^8). `cycles` stops after
1,000 cycles, or after the cycle that brings its member rows to 100,000 or more,
whichever comes first; it then logs a warning and writes the cycles found so far
with `Cycles_Complete` False on every row (True when every cycle was listed; the
DataFrame attr `cycles_complete` carries the same value). A partial sheet still
shows real cycles; `scc` lists every node that lies on any cycle, in linear time.
Exact betweenness runs one breadth-first search per node, O(nodes × links): a
20,000-item chain took about eight minutes. Up to 500 nodes it stays exact; above
that, `metrics` estimates it from 500 source nodes sampled with a fixed seed (so
repeat runs on the same graph agree) and logs a warning. `Betweenness_Sources` on
every row (and the attr `betweenness_sources`) is the number of sources used, which
equals the node count when the value is exact. These budgets have no command-line
options yet; `GraphAnalyzer` takes them as `max_cycles`, `max_cycle_rows` and
`centrality_sources`. With them, synthetic graphs of 10,000 and 50,000 items run
every step in about 10 seconds and 60 to 90 seconds respectively on a development
container, of which sampled betweenness takes about 40 seconds at 50,000 items.

Analyze logs each step as `Running step i/n`, then how long it took, and cycle
enumeration logs its count every 250 cycles. Ctrl-C during a step logs which step
was interrupted and exits with code 130; the report is published only after every
step, so nothing is written.

Analysis writes requested sheets, no index, with widths capped at 50. `all` runs
every step within the budgets above. The workbook describes itself in three sheets
before the results:

| Sheet | Content |
| --- | --- |
| `About` | `Item`/`Value` rows: purpose, how to read the report, tool version, the input file names, work items and links loaded, relations after normalization, links skipped for an absent endpoint or an unknown type, bridge items contracted or dropped, work items analyzed, result-sheet count, and `Complete` (`yes`, or `no:` with the partial sheets) |
| `Config` | `Setting`/`Value` rows: input and output file names (without their folder), `ado_url`, `effort_field`, `steps` as given, the resulting sheets, `bridges` and every budget (`max_indirect_edges`, `max_cycles`, `max_cycle_rows`, `centrality_sources`) |
| `Summary` | One row per result sheet: `Sheet` (a link to it), `Step`, `Subgraph`, `Rows`, a headline `Count` and what it `Counted`, `Complete` and `Note` |

The headline count is cycles for `cycles`, components for `scc` (the note adds how
many have more than one member), hops for `longest_chain`, change requests for the CR
sheets and work items for the others. `Complete` is False for `cycles` stopped at its
budget and for `metrics` with sampled betweenness, and the note says why; the note
also names a sheet's spill file. Result sheets follow in the requested order, except
that the one-row-per-pair `cr_impact_unpacked` and `cr_prereqs_unpacked` sheets
(on any subgraph) come last. No sheet records the time of the run, so repeat runs
on the same snapshot and settings give the same cells.

Every sheet has a filter on its heading row, which is frozen in view. Values in
columns whose name ends in `URL` (`WI_URL`, `CR_WI_URL`, ...) that start with
`http://` or `https://` are hyperlinks; they are empty without `--ado-url`. Excel
opens at most 65,530 hyperlinks per worksheet, so a sheet with more URL values keeps
them all as text and logs a warning.
Excel limits and column sizing are described under
[Files, errors and current limitations](#files-errors-and-current-limitations).

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

Excel limits are checked before anything is written: at most 1,048,576 rows
including the heading, 16,384 columns and 32,767 characters of text in a cell.
An export that requests `excel` with a Nodes or Edges sheet over a limit raises
`ExcelLimitError` (a `ValueError`) naming the sheet and limit, and writes none of
the requested formats; Parquet and CSV have no such limit. Analyze instead writes
each oversized sheet (typically `cr_impact_unpacked`, `cr_prereqs_unpacked` or
`cycles` on large graphs) to `<report stem>.<sheet>.csv` next to the workbook, in
the same atomic bundle, and that sheet holds one pointer row (`Spilled_To`,
`Rows`, `Columns`, `Reason`). `--overwrite` names only the report, so a spill
file is replaced only together with an existing report; a spill path that exists
without its report fails with `FileExistsError` before anything is written. A spill
file from an earlier run that this run does not spill is left as is. Step names longer than 31 characters are truncated to Excel's sheet-name
limit. Column widths come from the heading and at most 1,000 evenly spaced rows
(first and last included), capped at 50 characters, so sizing cost does not grow
with sheet length; a column whose values cannot be rendered as text keeps the
default width and is logged as a warning. The final log line reports the seconds
spent writing (including the workbook save) and sizing; `ADO_GRAPH_BENCH=1 pytest -s -k benchmark
tests/ado_graph/test_report.py` prints graph compute, write and sizing time for a
synthetic chain (`ADO_GRAPH_BENCH_NODES`, default 5,000).

## Verification and next steps

Tests use synthetic SDK-like objects and Parquet/XLSX files: CLI help and argument
validation, saved-query result shapes, edge normalization, scope/prerequisite
orientation, report publication, traversal through filtered bridges in both
relation modes, once-per-ID WIQL classification, omitted-ID reporting with
export's exit codes 4 and 5, bridge contraction, export normalization (literal
Excel text, identities, zoned dates, empty exports and schema errors) across all
formats, offline analysis without the network extras, CA-bundle cleanup, and
snapshots saved by export replaying into identical export files offline (also
without the network extras), with their provenance, consent and failure exits. No
actual ADO server was used. Remaining fixes and shared-code opportunities with ADO
scope triage are in [the backlog](../backlog/README.md#ado-graph-and-triage-sharing) (AG and SH tasks).
