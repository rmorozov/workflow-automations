"""Spans as CSV rows, and p50/p95 per team and span kind.

Percentiles are linear interpolation between closest ranks (the pandas and numpy
default, "type 7"), computed over per-work-item totals: a work item with two
``review_wait`` spans contributes their sum once.
"""

from __future__ import annotations

import csv
import io
import math
from collections import defaultdict

from workflow_automations.cycle_time.model import SECOND, format_time
from workflow_automations.cycle_time.spans import SPAN_KINDS, Result

SPAN_COLUMNS = [
    "wi_id",
    "team",
    "type",
    "kind",
    "round",
    "start_utc",
    "end_utc",
    "duration_s",
    "flags",
]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = math.floor(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _seconds(us: int) -> int | float:
    return us // SECOND if us % SECOND == 0 else us / SECOND


def spans_csv(results: list[Result]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(SPAN_COLUMNS)
    for result in results:
        item = result.item
        rows = []
        if result.queue:
            rows.append(("queue", 0, *result.queue, ()))
        rows += [(s.kind, s.round, s.start, s.end, s.flags) for s in result.spans]
        for kind, rnd, start, end, flags in rows:
            writer.writerow(
                [
                    item.id,
                    item.team,
                    item.type,
                    kind,
                    rnd,
                    format_time(start),
                    format_time(end),
                    _seconds(end - start),
                    ";".join(flags),
                ]
            )
    return out.getvalue()


def summary(results: list[Result]) -> list[dict]:
    """One row per team and kind (plus ``cycle``): work items, p50 and p95 in hours."""
    totals: dict[tuple[str, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for result in results:
        if result.cycle_time is None or "open" in result.flags:
            continue  # an open cycle has no cycle time yet
        team = result.item.team
        totals[(team, "cycle")][result.item.id] = result.cycle_time
        if result.queue:
            totals[(team, "queue")][result.item.id] = result.queue[1] - result.queue[0]
        for span in result.spans:
            totals[(team, span.kind)][result.item.id] += span.duration
    order = {kind: i for i, kind in enumerate(("queue",) + SPAN_KINDS + ("cycle",))}
    rows = []
    for (team, kind), per_item in sorted(
        totals.items(), key=lambda kv: (kv[0][0], order[kv[0][1]])
    ):
        values = [v / (3600 * SECOND) for v in per_item.values()]
        rows.append(
            {
                "team": team,
                "kind": kind,
                "work_items": len(values),
                "p50_h": round(percentile(values, 0.5), 2),
                "p95_h": round(percentile(values, 0.95), 2),
            }
        )
    return rows


def summary_markdown(rows: list[dict]) -> str:
    lines = ["| Team | Kind | Work items | p50 h | p95 h |", "| --- | --- | ---: | ---: | ---: |"]
    for row in rows:
        cells = [row["team"], row["kind"], row["work_items"], row["p50_h"], row["p95_h"]]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    return "\n".join(lines) + "\n"


SUMMARY_COLUMNS = ["team", "kind", "work_items", "p50_h", "p95_h"]


def summary_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, SUMMARY_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()
