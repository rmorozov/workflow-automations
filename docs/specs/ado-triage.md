# ADO scope triage specification, v1

Status: implemented initial contract. Entry points: `ado-triage`,
`python scripts/ado_triage.py`, or `python -m workflow_automations.ado_triage`.

## Goal

Turn an Azure DevOps (ADO) saved query of change requests into a multi-sheet Excel
report that supports scope triage across several large teams. The report answers
"what do we discuss first, and what is the action?", not only "what is true":
every heuristic produces a flag, a weight and a recommended action, items are
ranked into a meeting agenda, and week-over-week deltas expose problems that
persist despite management attention.

## Networking and credentials

This tool requires networking by design: it reads from an **on-premise Azure
DevOps Server** collection with **Kerberos** (the running user's ticket; Windows
SSPI or Linux GSSAPI). It never writes to ADO, stores no credentials, and uses no
personal access tokens. Network access is confined to `fetch_snapshot`; all other
processing is a deterministic, offline transformation of the fetched snapshot,
an optional capacity workbook and an optional previous report.

Install `workflow-automations[ado-triage,ado-kerberos]`. The `ado-kerberos` extra
(`requests-kerberos`) is separate because on Linux it builds `gssapi` and needs
system Kerberos headers (for example `libkrb5-dev` or `krb5-devel`); on Windows it
installs without them. Tests and CI do not need it.

## Inputs

**Query.** `--query-id` names a saved query. Flat, one-hop and tree queries are all
accepted; every work item referenced by the result is fetched with relations.
Work items are fetched in batches of 200. If any queried item cannot be fetched,
the run fails (exit 5): a report missing part of the scope would look complete.

**Fields** read per work item: `System.Id`, `System.Parent`, `System.WorkItemType`,
`System.AreaPath`, `System.State`, `System.Title`, `System.AssignedTo`,
`System.ChangedDate`, `Microsoft.VSTS.Scheduling.{OriginalEstimate, CompletedWork,
RemainingWork}`, and custom dates `my.EstimationExpectedDate`,
`my.EstimationReadyDate`, `my.UATExpectedDate`, `my.UATReadyDate`. Absent fields are
treated as empty: missing hours are 0, missing dates are blank. Dates are converted
to naive UTC because Excel cannot store time zones.

**Links** read: Child (`Hierarchy-Forward`), Predecessor (`Dependency-Reverse`),
Successor (`Dependency-Forward`), Blocks (`Blocks-Forward`) and Blocked By
(`Blocks-Reverse`). Linked items outside the query, and parents, are fetched for
titles and AreaPaths only. If they are unavailable (deleted or no permission) they
are labelled `UNKNOWN` and a warning is recorded; the run continues.

**Capacity** (`--capacity-file`, optional): XLSX with sheet `dev_per_area_capacity`.
Column A holds AreaPaths (`Project\Dept\Team`, surrounding spaces ignored); other
columns are months `YYYY-MM`, including headers that Excel converted to dates; cells
are hours. The window is `--capacity-months` months (default 2) starting at
`--capacity-start-month` or, by default, the month after `--as-of`. Every month in
the window must exist, AreaPaths must be unique, and the window must contain some
numeric value; otherwise the run fails (exit 2). Blank cells count as 0. A work item
uses the capacity of its deepest configured ancestor AreaPath (`Matched_Capacity_Level`);
`Monthly_Capacity` is that total divided by the window length.

**Previous report** (`--previous-file`, optional): an earlier output of this tool,
or of the legacy script (any sheet named `* Master Data` with a `System.Id` column).
It enables deltas. A legacy report without flags gives `Delta_Status` but restarts
persistence and has no resolved list; this is recorded as a warning.

All local inputs are validated before ADO is contacted.

## Heuristics

One registry (`HEURISTICS`) defines every flag. Per-item sheets, flag columns,
scores, agenda and team rollups all derive from it.

| Flag | Kind | Weight | Condition | Recommended action |
| --- | --- | --- | --- | --- |
| DATE_DRIFT | risk | 3 | UAT expected before as-of + timebox, and UAT ready blank or later than expected | Re-confirm or renegotiate UAT date |
| BLOCKS_OTHERS | risk | 3 | ≥1 Successor or Blocks link from this item | Prioritize to unblock downstream items |
| EXCEEDS_MONTH_CAP | risk | 3 | Remaining work > Monthly_Capacity | Split CR or spread across months |
| EST_DEBT_DATE | risk | 2 | Estimation expected date passed, ready date blank | Chase estimation owner; set hard date |
| EST_DEBT_HOURS | risk | 2 | Original estimate 0 and remaining work 0 | Get hours estimate before committing |
| STALE | risk | 2 | No change for ≥ `--stale-days` days (default 21) | Ping owner; confirm item is alive |
| UNASSIGNED | risk | 2 | `AssignedTo` blank | Assign an owner |
| OVER_ESTIMATE | risk | 1 | Completed > original estimate > 0 | Re-baseline estimate; review scope creep |
| DEFER_CANDIDATE | opportunity | 1 | No completed work, blocks nothing, UAT blank or beyond timebox | Propose cut or defer |
| QUICK_WIN | opportunity | 1 | Completed > 0 and remaining 1–20 h | Pull into sprint to finish |

`Attention_Score` is the sum of matched **risk** weights. Opportunities are listed
in `Opportunity_Flags` and actions but never affect score, persistence or
resolution. The timebox is `--timebox-days` (default 60) after `--as-of`.

## Deltas

With a previous report: `Delta_Status` is `NEW` or `CARRIED`; `Flag_Persist_Count`
is the number of consecutive reports in which the item carried at least one risk
flag (previous count + 1, 1 when newly flagged, 0 when clean). Without one, status
is `N/A` and persistence is 1 for flagged items. Items risk-flagged previously and
now clean (`Flags cleared`) or absent (`Dropped from query`) are listed as resolved.

## Output

Every sheet has a one-line purpose in A1, headers on row 3, data from row 4 and
frozen panes. Sheet names fit Excel's 31-character limit. Sheets that would be empty
are omitted and listed on the Config sheet, except Summary, Agenda and Master Data.

| Sheet | Content |
| --- | --- |
| 0. Config & Legend | As-of date, inputs, capacity window, thresholds, omitted sheets, warnings, flag legend |
| 1. Executive Summary | Per team: CR count, remaining work, capacity, variance, `Load_Pct`, `RAG`, rollup counts, `Team_Attention_Score` |
| 2. Triage Agenda | Top `--agenda-size` (default 20) items by score, then persistence, then remaining work |
| 3. Master Data | All items with enrichment, flag, score, action and delta columns |
| 4. Capacity Triage | Remaining work vs capacity per `Matched_Capacity_Level` |
| 5–18 | One sheet per view: estimation debt (dates, hours), over estimation, minor progress (<16 h logged), date drift, top blockers, cross-team dependencies (non-Child links between AreaPaths), deferred candidates, quick wins, all relations, top 20% remaining work per AreaPath (at least one), exceeds monthly capacity, unassigned, stale |
| 19. Resolved Since Last Report | See Deltas |

`Load_Pct` is remaining work as a percentage of capacity. `RAG`: RED above 100,
AMBER from 85, GREEN below, GREY when capacity is undefined. The summary is sorted
RED, AMBER, GREY, GREEN, then by team score; RAG cells are colored, and score and
staleness columns use color scales.

`--profile exec` writes only Config, Summary, Agenda, Capacity Triage and Resolved.
`--format csv` writes Master Data only and rejects `--previous-file` and
`--profile exec`, which require the workbook.

Outputs are written to a sibling temporary file and atomically renamed. An existing
output requires `--overwrite`; the output may not be the capacity or previous file.
Inputs are never modified.

## CLI and exit codes

`ado-triage export --help` lists all options. `--file` is accepted as an alias of
`--output` for compatibility with the legacy script. `--as-of YYYY-MM-DD` fixes the
report date (default: now), making date-based flags reproducible.

```bash
ado-triage export --url https://ado.example.com/tfs/Collection/Project \
  --query-id 00000000-0000-0000-0000-000000000000 \
  --capacity-file capacity.xlsx --previous-file work/triage-2026-09-24.xlsx \
  --output work/triage-2026-10-01.xlsx
```

| Exit | Meaning |
| --- | --- |
| 0 | Report written (warnings, if any, are printed and listed on the Config sheet) |
| 2 | Invalid options, capacity, previous report, or existing output without `--overwrite` |
| 3 | File IO failure |
| 4 | Query returned no work items; nothing written |
| 5 | ADO connection, authentication or query failure; nothing written |

## Design rationale

These decisions came from reviewing a predecessor script; they constrain changes.

- **Blocker direction.** Predecessor (`Dependency-Reverse`) means *this item depends
  on another*. Counting it as blast radius ranked blocked items as top blockers.
  Only Successor and Blocks count; the label list exists once (`BLAST_RADIUS_LABELS`).
- **One registry.** The predecessor duplicated the blocking-link list in two views,
  which drifted apart. Add heuristics only to `HEURISTICS`.
- **Risk vs opportunity.** When `DEFER_CANDIDATE` counted as a flag, most untouched
  items stayed "flagged", so cleared risks never appeared resolved and persistence
  became noise. Good-news signals must be `opportunity`.
- **Root items** come from `System.Parent`, not query shape; flat queries used to
  mark every item as a root.
- **Fail rather than mislead.** Capacity windows with missing months previously fell
  back to summing all columns, and failed fetch batches were logged and skipped.
  Both produced plausible but wrong reports; both now fail.
- **Network boundary.** `fetch_snapshot` returns plain data (`Snapshot`), so tests
  and future offline modes exercise the full pipeline without ADO.

## Verification and limits

Tests use synthetic snapshots and generated workbooks to cover flag kinds, scoring,
blocker direction, root detection, unknown linked items, hierarchical capacity,
RAG, the top-20% rule, capacity header normalization and validation, a two-report
delta cycle, legacy previous reports, workbook layout, profiles, CSV, overwrite
protection, input-before-network validation and exit codes. `fetch_snapshot` is not
covered by automated tests because it requires an ADO server.

Hour thresholds (16 h, 1–20 h), flag weights and RAG bands are fixed in this
version. Field names in the `my.*` namespace are fixed. Only one previous report is
compared. See the [backlog](../backlog.md) for proposed extensions.
