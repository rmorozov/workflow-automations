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

from workflow_automations.cycle_time.model import Build, format_time
from workflow_automations.cycle_time.spans import Result

MODES = ("calendar", "aligned")
US = 1  # model times are already microseconds, the trace's unit


def _gerrit_name(kind: str, event: Any) -> str:
    if kind == "patchset":
        return f"{event.change} ps{event.patchset or '?'}" + (" (WIP)" if event.wip else "")
    if kind == "vote":
        label = "CR" if event.label == "Code-Review" else event.label
        return f"{event.change} {label}{event.value:+d}"
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


def _end(build: Build, cutoff: int) -> int:
    """Display end of a build: its finish, or the observation cutoff while unfinished."""
    return build.finished_at if build.finished_at is not None else max(cutoff, build.queued_at)


def _pack(builds: tuple[Build, ...], cutoff: int) -> list[list[Build]]:
    """Builds whose lifetimes overlap go on separate lanes, so slices never cross."""
    lanes: list[list[Build]] = []
    for build in sorted(builds, key=lambda b: (b.queued_at, b.id)):
        for lane in lanes:
            if _end(lane[-1], cutoff) <= build.queued_at:
                lane.append(build)
                break
        else:
            lanes.append([build])
    return lanes


def _build_slices(
    writer: _Writer, pid: int, tid: int, build: Build, cutoff: int, shift: int, lo: int | None
) -> None:
    """Queue and run slices of one build; ``lo`` clips each phase to the visible window."""

    def emit(name: str, start: int, end: int, args: dict) -> None:
        if lo is not None:
            if end <= lo:
                return
            if start < lo:
                start, args = lo, {**args, "clipped": True}
        if end > start:
            writer.slice(pid, tid, name, start + shift, end + shift, args, "ci")

    start = build.started_at
    if start is None:
        start = build.finished_at  # finished without starting (canceled while queued)
    queue_end = start if start is not None else _end(build, cutoff)
    emit("queued", build.queued_at, queue_end, {"build": build.id, "ongoing": start is None})
    if build.started_at is not None:
        ongoing = build.finished_at is None
        name = f"build {build.id} " + ("running" if ongoing else str(build.result))
        args = {"build": build.id, "result": build.result, "ongoing": ongoing}
        emit(name, build.started_at, _end(build, cutoff), args)


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
        base = order * 100
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
            sort = base + 3
            for topic in result.topics:
                for lane_no, lane in enumerate(_pack(topic.builds, result.cutoff), start=1):
                    suffix = f" #{lane_no}" if lane_no > 1 else ""
                    ci = writer.lane(pid, f"WI {item.id} · CI {topic.name}{suffix}", sort)
                    sort += 1
                    for build in lane:
                        clip = result.cycle_start if mode == "aligned" else None
                        _build_slices(writer, pid, ci, build, result.cutoff, shift, clip)
    return {"traceEvents": writer.events, "displayTimeUnit": "ms", "otherData": {"mode": mode}}


def dumps(results: list[Result], mode: str = "calendar") -> str:
    return json.dumps(export(results, mode), indent=None, separators=(",", ":"))
