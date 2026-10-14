# Shared ADO raw snapshot specification

Both ADO tools fetch work items from the same saved queries. A raw snapshot keeps
what ADO returned, as plain JSON data, so a run can be replayed offline into the
tool that saved it, and a complete scope can be reused by the other tool. The
format lives in `workflow_automations.ado_common.snapshot`; each tool converts to
and from it with its own adapter. Nothing here contacts ADO.

The command-line options that save and replay snapshots are added separately:
ado-graph's export (AG-012) and ado-triage (TR-009). This specification describes
the format and the adapters they use.

## File format, version 1

One UTF-8 JSON object:

| Key | Content |
| --- | --- |
| `schema` | Always `workflow-automations/ado-snapshot` |
| `version` | Integer, currently `1` |
| `producer` | `ado-graph` or `ado-triage` |
| `captured_at` | ISO 8601 UTC time the producer recorded |
| `source` | `base_url`, `project`, `query_id` of the run |
| `filters` | Producer's scope filters, each a list of strings |
| `options` | Other producer settings needed for replay |
| `roots` | IDs the saved query returned, in query order |
| `items` | Fully fetched work items in fetch order: `id`, `fields` (every field as ADO returned it), `relations` (`rel`, `url` or null) |
| `linked` | Partial fields of items fetched only for labels, keyed by ID as a string |
| `omitted` | Requested IDs that ADO did not return |
| `warnings` | Non-fatal degradations recorded while fetching |

A snapshot is *filtered* when any `filters` list is non-empty: its items were
chosen by type or project, so it is not the complete set of reachable items.

Loading refuses (`SnapshotError`) another `schema`, any `version` other than 1,
non-integer or boolean IDs, an ID listed twice in `items` or `roots`, `linked`
keys that are not canonical decimal IDs, filters that are not string lists, `NaN`
or `Infinity`, and files that are not JSON.
Saving refuses an existing file unless `overwrite` is set, writes through a
sibling temporary file so a failure leaves the old file unchanged, and refuses
field values that are not JSON data or anything loading would refuse.

## ado-graph adapter

`ado_graph.snapshot.snapshot_from_work_items` records every fetched SDK work item,
in or out of scope, with the export's case-normalised `skip_types` and
`include_projects` filters and its `filtered_relations` option, the seed IDs as
`roots`, and the omitted IDs.

`frames_from_snapshot` applies the export's scope rule to those items and returns
the node and edge tables the export writes for them (`In_Scope`, renamed columns,
deduplicated edges); `replay_export` writes them in the requested formats for
`analyze`. Both refuse an `ado-triage` snapshot, which did not walk relations, and
a snapshot with an omitted root, for which the export writes nothing (exit 5).
`replay_export` logs and returns the omitted linked IDs: when there are any, the
graph is incomplete, as the export's exit 4 reports.

## ado-triage adapter

`ado_triage.snapshot_to_raw` stores a triage `Snapshot`: its items are the roots,
linked items are the `linked` entries and parent titles are the `parent_titles`
option, kept apart because one ID can be fetched as a linked item, a parent, both
or neither. `snapshot_from_raw` rebuilds the same `Snapshot`, so it gives the same
report:

- A filtered snapshot is refused with a validation error, whichever tool saved it:
  a graph export filtered by type or project is not a complete triage scope.
- Every root must be among `items`; otherwise the scope is incomplete and refused.
- The triage scope is the roots only. Other items and `linked` entries supply titles
  and AreaPaths for relation targets outside the scope and titles for parents.
- For another tool's snapshot, relation targets and parents it does not contain
  are recorded as warnings, as a triage fetch records unavailable items.

## Verification

Tests in `tests/ado_common/` cover the round trip, validation and safe writes;
an export run on synthetic SDK objects whose snapshot replays into identical
Parquet files and analysis workbook in `stop` and `follow` modes; a triage snapshot
that rebuilds an identical report; and a filtered graph snapshot refused as a
triage scope while an unfiltered one gives the query's items.
