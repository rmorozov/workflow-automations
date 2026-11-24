"""Chrome JSON trace export, which the Perfetto UI opens directly.

Stdlib only. Each team is a process; each work item has a lane for its spans (cycle,
round, span nested three deep), a lane for its ADO states, a lane for Gerrit events
and one CI lane per linked topic. ``aligned`` mode shifts each work item so that its
cycle starts at 0 and sorts lanes by cycle time, longest first.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from workflow_automations.cycle_time.model import format_time
from workflow_automations.cycle_time.spans import Result

MODES = ("calendar", "aligned")
US = 1_000_000


def _gerrit_name(kind: str, event: Any) -> str:
    if kind == "patchset":
        return f"{event.change} ps{event.patchset or '?'}" + (" (WIP)" if event.wip else "")
    if kind == "vote":
        return f"{event.change} CR{event.value:+d}"
    return f"{event.change} {kind}"


class _Writer:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.pids: dict[str, int] = {}
        self.next_tid = 1

    def pid(self, team: str) -> int:
        if team not in self.pids:
            self.pids[team] = len(self.pids) + 1
            self.events.append(
                {
                    "ph": "M",
                    "name": "process_name",
                    "pid": self.pids[team],
                    "args": {"name": team or "No team"},
                }
            )
        return self.pids[team]

    def lane(self, pid: int, name: str, sort: int) -> int:
        tid = self.next_tid
        self.next_tid += 1
        self.events.append(
            {"ph": "M", "name": "thread_name", "pid": pid, "tid": tid, "args": {"name": name}}
        )
        self.events.append(
            {
                "ph": "M",
                "name": "thread_sort_index",
                "pid": pid,
                "tid": tid,
                "args": {"sort_index": sort},
            }
        )
        return tid

    def slice(
        self, pid: int, tid: int, name: str, start: int, end: int, args: dict, cat: str
    ) -> None:
        self.events.append(
            {
                "ph": "X",
                "name": name,
                "cat": cat,
                "pid": pid,
                "tid": tid,
                "ts": start * US,
                "dur": (end - start) * US,
                "args": args,
            }
        )

    def instant(self, pid: int, tid: int, name: str, at: int, args: dict) -> None:
        self.events.append(
            {"ph": "i", "s": "t", "name": name, "pid": pid, "tid": tid, "ts": at * US, "args": args}
        )


def _rounds(result: Result) -> Iterator[tuple[int, int, int]]:
    current = None
    for span in result.spans:
        if current and current[0] == span.round:
            current = (span.round, current[1], span.end)
        else:
            if current:
                yield current
            current = (span.round, span.start, span.end)
    if current:
        yield current


def export(results: list[Result], mode: str = "calendar") -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    writer = _Writer()
    started = [r for r in results if r.cycle_start is not None]
    if mode == "aligned":
        started.sort(key=lambda r: (-(r.cycle_time or 0), r.item.id))
    else:
        started.sort(key=lambda r: (r.cycle_start, r.item.id))
    for order, result in enumerate(started):
        item = result.item
        shift = -result.cycle_start if mode == "aligned" else 0
        lo = 0 if mode == "aligned" else None

        def keep(t: int, lo: int | None = lo, shift: int = shift) -> bool:
            return lo is None or t + shift >= lo

        pid = writer.pid(item.team)
        base = order * 10
        tid = writer.lane(pid, f"WI {item.id} {item.type}", base)
        args = {"wi_id": item.id, "flags": sorted(result.flags), **result.metrics}
        if result.critical_topic:
            args["topic"] = result.critical_topic
        if result.queue and mode == "calendar":
            writer.slice(pid, tid, "queue", *result.queue, {"wi_id": item.id}, "queue")
        writer.slice(
            pid,
            tid,
            f"WI {item.id}: {item.title}" if item.title else f"WI {item.id}",
            result.cycle_start + shift,
            result.cycle_end + shift,
            args,
            "cycle",
        )
        for rnd, start, end in _rounds(result):
            writer.slice(
                pid,
                tid,
                f"round {rnd}",
                start + shift,
                end + shift,
                {"wi_id": item.id, "round": rnd},
                "round",
            )
        for span in result.spans:
            span_args = {
                "wi_id": item.id,
                "round": span.round,
                "start_utc": format_time(span.start),
                "end_utc": format_time(span.end),
                "flags": list(span.flags),
            }
            writer.slice(
                pid, tid, span.kind, span.start + shift, span.end + shift, span_args, "span"
            )

        ado = writer.lane(pid, f"WI {item.id} · ADO", base + 1)
        states = item.states
        for entry, nxt in zip(states, list(states[1:]) + [None], strict=False):
            end = nxt.at if nxt else result.cycle_end
            if end > entry.at and keep(entry.at):
                writer.slice(
                    pid, ado, entry.state, entry.at + shift, end + shift, {"rev": entry.rev}, "ado"
                )
            elif keep(entry.at):  # the final Closed, or a state left in the same second
                writer.instant(pid, ado, entry.state, entry.at + shift, {"rev": entry.rev})

        if result.topics:
            gerrit = writer.lane(pid, f"WI {item.id} · Gerrit", base + 2)
            for topic in result.topics:
                for change in topic.changes:
                    for event in change.events:
                        if keep(event.at):
                            name = _gerrit_name(event.kind, event)
                            writer.instant(pid, gerrit, name, event.at + shift, {"topic": topic.id})
            for k, topic in enumerate(result.topics):
                ci = writer.lane(pid, f"WI {item.id} · CI {topic.name}", base + 3 + k)
                for build in topic.builds:
                    start = build.started_at if build.started_at is not None else build.finished_at
                    if start is not None and start > build.queued_at and keep(build.queued_at):
                        writer.slice(
                            pid,
                            ci,
                            "queued",
                            build.queued_at + shift,
                            start + shift,
                            {"build": build.id},
                            "ci",
                        )
                    if (
                        build.started_at is not None
                        and build.finished_at is not None
                        and keep(build.started_at)
                    ):
                        writer.slice(
                            pid,
                            ci,
                            f"build {build.id} {build.result}",
                            build.started_at + shift,
                            build.finished_at + shift,
                            {"build": build.id, "result": build.result},
                            "ci",
                        )
    return {"traceEvents": writer.events, "displayTimeUnit": "ms", "otherData": {"mode": mode}}


def dumps(results: list[Result], mode: str = "calendar") -> str:
    return json.dumps(export(results, mode), indent=None, separators=(",", ":"))
