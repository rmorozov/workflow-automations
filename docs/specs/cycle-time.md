# Cycle time specification, v0

Status: first slice, built on synthetic fixtures. Entry points: `cycle-time`,
`python scripts/cycle_time.py`, or `python -m workflow_automations.cycle_time`.
It needs no extras: the package is stdlib-only and offline.

## Goal

Reconstruct each ADO Task's and Bug's path from first Active to Closed, split it into
spans that say what the work item was waiting on, and show the result as a Perfetto
trace (one work item as a flame chart, or a team aligned at zero) and as p50/p95 per
team and span kind. The tool only reads. It never writes to ADO, Gerrit or CI.

This version reads hand-written normalized input files. Ingest from ADO, Gerrit,
ADO Pipelines and the build-log share comes later (see
[Planned layers](#planned-layers)).

## Lifecycle

ADO states mean:

| State | Meaning |
| --- | --- |
| Proposed | Waiting; may be assigned or unassigned |
| Active | In development |
| Resolved | On review and under the CI quality gate |
| Closed | Merged into the branch |

ADO is the master clock for the **cycle bounds only**:

- The cycle starts at the first Active entry.
- It ends at the final Closed entry: the last state, when that state is Closed. While
  the work item is not Closed, the cycle ends at the input's `as_of` and is flagged
  `open`.
- `queue` runs from the first recorded state (creation or Proposed) to the first
  Active entry. It is reported, but it is outside the cycle.

Inside the cycle, Gerrit and CI events decide the spans. ADO states are compared with
them (`ado_mismatch_s`), not used to clamp them. Review and CI happen while the work
item is Resolved, so clamping into Active windows would erase them.

## Input file

One UTF-8 JSON object, the shape the normalized layer will have once ingest exists:

| Key | Content |
| --- | --- |
| `schema` | Always `workflow-automations/cycle-time-input` |
| `version` | `1` |
| `as_of` | End of the data, for open cycles |
| `work_items` | `id`, `type`, `team`, `title`, `states`: `{rev, state, at}` |
| `topics` | `id` (stable synthetic ID), `name` (the topic string), `wi_ids`, `changes`, `builds` |
| `changes[]` | `number`, `repo`, `events`: `{at, kind, ...}` |
| `builds[]` | `id`, `queued_at`, `started_at`, `finished_at`, `result` (`pass`, `fail`, `infra`, `canceled`), optional `tested`: change number → patchset number |

Change event kinds: `patchset` (`patchset`, `wip`, optional `keeps_votes`), `ready`
(Ready-for-review), `wip` (set back to WIP), `vote` (Code-Review `value` −2..2,
optional `reviewer`), `merged`, `abandoned`. A vote with a `reviewer` (a pseudonymous
key, never a name) sets that reviewer's vote, and `0` withdraws it. A vote without one
is the change's effective label and replaces all votes. Any record of the wrong JSON
type is refused with its path (for example `work_items[0].states[0]`).

Times are ISO 8601 with an explicit zone; a time without a zone is refused. They are
kept as integer microseconds, so sub-second order between sources survives. States are
ordered by `rev`, never by timestamp, so two changes in one second stay in order.
Other keys (`description`, `expected`) are ignored, so scenario files are valid input.
Several files can be passed at once; work item and topic IDs must not repeat across
them. Each work item keeps its own file's `as_of` as its observation cutoff, so adding
a later snapshot never extends an open item's last span or changes its rank.

The input holds **no person names or accounts**. Ingest will pseudonymize people
before this layer, and spans, traces and reports are team-level only.

## Span rules

The cycle is cut at every ADO state change, Gerrit event and build queue, start and
finish time. Each piece gets one label from the state after all events up to its
start, and equal neighbours merge. Spans therefore **partition** the cycle: they are
contiguous and add up to the cycle time exactly. `validate` checks this for every
work item, like a ledger in which every hour is booked to some account.

Labels, first match wins:

1. ADO is Closed (before the final close): `closed_interim`. ADO is Proposed:
   `parked`.
2. Every live change is merged: `close_lag`. Only a reopen (ADO Active again after a
   Closed) makes it author time; an item that stays Active through the merge is
   still `close_lag`.
3. No change yet, a pending change in WIP, a pending change with an outstanding
   negative vote, or a failed build of the current revision: `coding` in round 1,
   `rework` afterwards.
4. A build of the current revision is running: `ci_run`. One is queued: `ci_queue`.
5. A change lacks +2: `review_wait`.
6. Approved, and the last build since the latest patchset passed: `submit_wait`.
7. Otherwise (approved, no build queued yet): `unattributed`. This is where the
   time between +2 and the CI trigger shows up.

Details:

- A build counts only for the revision and composition it tested, checked again at
  every step rather than remembered. With `tested`, every tested change must still be
  live (pending or merged) at the tested patchset, every pending change must be
  tested, and a change merged after the build was queued must have been tested too;
  so an upload, an abandon, a change first seen through any event (a merge included,
  since it proves the change existed), a merge of a later patchset, or a merge of an
  untested sibling all retire it. A build that is already retired when it finishes is
  dropped, so stale builds cost nothing later. Without `tested`, the build must have been queued
  strictly after the last upload, new change or abandon: the same instant is
  ambiguous and counts as stale. A stale build's running time and verdict are both
  ignored; the verdict is the latest finished pass or fail that is still current.
- A change is approved while some vote is +2 and none is −2. A negative vote stays
  with its change until that change gets a new patchset or a Ready-for-review, or
  the reviewer changes it; uploads to other changes don't clear it.
- A new patchset drops the change's +2 unless the event says `keeps_votes`.
- An `infra` or `canceled` build asks nothing of the author; a `fail` does.
- A running build outranks a missing +2 (rule 4 before 5): when CI starts at
  Ready-for-review, the overlap counts as CI time.
- A round ends when an author span follows a gate span (`review_wait`, `ci_*`,
  `submit_wait`, `unattributed`), `close_lag` or `closed_interim`.
- A gate that a non-WIP upload started, rather than Ready-for-review, flags the
  author span before it `inferred_patchset`: its end is the upload, not a declared
  boundary.
- Several topics per work item: the **critical topic** sets the spans. A topic is
  complete when every live change is merged by the cycle end; the complete topic
  that became complete last wins (by the event that completed it, which may be an
  abandon), else the topic with the latest activity. Only events up to
  the cycle end count, so a build that finishes after the close, or one merged member
  of an unfinished topic, can't displace a completed topic. The others are shown on
  their own lanes. The work item is flagged `multi_topic`; a topic linked to several work
  items flags them `shared_topic`.
- A work item with no topic is split by ADO alone: Active is `coding` / `rework`,
  Resolved is `gate`, and it is flagged `ado_only`.

Work-item flags: `open`, `reopened`, `not_started`, `ado_only`, `multi_topic`,
`shared_topic`, `activity_before_active`. Metrics: `rounds`, `ado_mismatch_s`
(seconds where ADO said Active during a gate span, or Resolved during an author span),
`activity_before_active_s` (earliest Gerrit event or build queue, start or finish
before the first Active entry).

## Outputs

- `cycle-time spans INPUT... [-o FILE]`: CSV, one row per span plus the `queue` row
  (round 0): `wi_id, team, type, kind, round, start_utc, end_utc, duration_s, flags`.
  Times carry a fraction only when they have one.
- `cycle-time trace INPUT... -o FILE [--mode calendar|aligned]`: a Chrome JSON trace
  that [ui.perfetto.dev](https://ui.perfetto.dev) opens directly (the UI runs in the
  browser and does not upload the file). Each team is a process. Each work item has a
  span lane (cycle, round and span nested at depths 0, 1, 2), an ADO lane, a Gerrit
  lane of instants and CI lanes per linked topic. Builds whose lifetimes overlap go
  on separate CI lanes (`#2`, `#3`), so slices never cross. A build still queued or
  running at the cutoff is drawn up to the cutoff with `ongoing: true`. In `aligned`
  mode each queue and run phase is clipped at the cycle start (`clipped: true`). Every span
  keeps its UTC times and flags in args. `aligned` shifts each work item to start at 0, sorts the longest
  first and drops `queue` and anything before the cycle.
- `cycle-time report INPUT... [-o FILE] [--csv]`: p50 and p95 hours per team and
  kind, plus `cycle`. Percentiles interpolate linearly between ranks (the pandas
  default) over per-work-item totals, and each kind counts only the work items that
  have it. Open cycles are left out.

An existing output file is refused unless `--overwrite` is given. Bad input exits 2.

## Planned layers

The target pipeline has three stored layers, each derived from the one before:

1. **Landing**: verbatim API payloads (ADO work item updates, Gerrit changes with
   messages, pipeline runs) and log excerpts with file hashes. Append-only, so a
   parser fix never needs a re-fetch, and logs survive share retention.
2. **Normalized**: the input file above (later SQLite tables), with links parsed
   from commit footers and topic names, comment ↔ build joins, and pseudonymized
   people.
3. **Facts**: spans, instants and links. Traces and reports are projections of
   facts. Perfetto track layout lives only in the exporter, because calendar and
   aligned modes need different layouts.

The follow-ups are backlog tasks CT-001..CT-009.

## Scenarios

`tests/cycle_time/scenarios/*.json` are input files with an `expected` block: spans,
flags, queue and metrics derived by hand. They are the executable form of the span
rules:

| File | Shows |
| --- | --- |
| `01_happy_path` | WIP, Ready, +2, then CI; `unattributed` +2 → queue gap |
| `02_ci_on_ready` | CI during review; `close_lag` |
| `03_review_rejected_forgot_wip` | −1, rework ended by a non-WIP upload, ADO late |
| `04_ci_fail_after_infra_retry` | infra retry, real failure, WIP-on rework |
| `05_multi_change_topic` | two changes: last Ready, all +2, all merged |
| `06_two_topics` | critical topic |
| `07_bug_without_code` | `ado_only` |
| `08_reopened` | `closed_interim`, rework after reopen |
| `09_open_cycle` | `open`, left out of percentiles |
| `10_resolved_early` | `ado_mismatch_s`, activity before Active |
| `11_stale_build_verdict` | a pass of the previous patchset doesn't approve the new one |
| `12_rejection_on_other_change` | −1 on A is not cleared by an upload on B |
| `13_active_through_merge` | no reopen, so post-merge time is `close_lag` |
| `14_ado_only_reopen` | Active → Closed → Active starts round 2 |
| `15_overlapping_and_running_builds` | overlapping and unfinished builds in the trace |
| `16_abandoned_tested_change` | abandoning a tested change before or after the pass |
| `17_withdrawn_approval` | a withdrawn sole +2 reopens review; a second +2 holds |
| `18_change_first_seen_by_ready` | a change first seen through Ready retires the earlier pass |
| `19_stale_build_after_merge` | a later patchset merged while CI ran keeps that build stale |
| `20_subsecond_build_order` | 0.8 s before an upload, or the same instant, is not current |
| `21_sibling_merged_after_build` | a sibling merged after the queue time must have been tested |
| `22_merge_is_the_only_event` | a change seen only through its merge still counts as new |

`cycle-time trace tests/cycle_time/scenarios/*.json -o demo.json` builds a trace of
all of them.

## Verification and open questions

`tests/cycle_time/` checks every scenario and the partition, input refusals,
strict nesting per trace lane in both modes, percentiles and the CLI.

Open, to settle with real data:

- The CI trigger point (+2, Ready-for-review or every patchset). The rules handle
  all three, and the answer shows how much time lands in `unattributed`.
- Whether a running build should outrank `review_wait` (rule 4 before 5).
- Whether Closed is set automatically on merge (expected `close_lag` ≈ 0).
- Wall time only for now; business time will reuse the team-capacity calendar
  (CT-006).
