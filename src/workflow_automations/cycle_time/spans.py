"""Split each work item's cycle into spans that account for every second.

ADO bounds the cycle: it starts at the first Active entry and ends at the final
Closed entry (or at ``as_of`` while the item is open). Inside the cycle, the
critical topic's Gerrit and CI events decide what the work item is waiting on at
each moment, and ADO states are only cross-checked. The rules are in
docs/specs/cycle-time.md#span-rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from workflow_automations.cycle_time.model import Build, ChangeEvent, Input, Topic, WorkItem

AUTHOR_KINDS = ("coding", "rework")
GATE_KINDS = ("review_wait", "ci_queue", "ci_run", "submit_wait", "unattributed", "gate")
SPAN_KINDS = AUTHOR_KINDS + GATE_KINDS + ("close_lag", "closed_interim", "parked")
# An author span after one of these starts a new round.
ROUND_BREAKERS = GATE_KINDS + ("close_lag", "closed_interim")


@dataclass(frozen=True)
class Span:
    kind: str
    round: int
    start: int
    end: int
    flags: tuple[str, ...] = ()

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass
class Result:
    item: WorkItem
    cycle_start: int | None = None
    cycle_end: int | None = None
    queue: tuple[int, int] | None = None
    spans: list[Span] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    metrics: dict[str, int] = field(default_factory=dict)
    critical_topic: str | None = None
    topics: list[Topic] = field(default_factory=list)

    @property
    def cycle_time(self) -> int | None:
        if self.cycle_start is None or self.cycle_end is None:
            return None
        return self.cycle_end - self.cycle_start


# ---------------------------------------------------------------------------
# ADO state timeline
# ---------------------------------------------------------------------------


def _ado_state_at(item: WorkItem, t: int) -> str:
    state = item.states[0].state
    for entry in item.states:
        if entry.at <= t:
            state = entry.state
        else:
            break
    return state


def _bounds(item: WorkItem, as_of: int, result: Result) -> bool:
    states = item.states
    first_active = next((s for s in states if s.state == "Active"), None)
    if first_active is None:
        result.flags.add("not_started")
        return False
    result.cycle_start = first_active.at
    if states[0].at < first_active.at:
        result.queue = (states[0].at, first_active.at)
    closes = [s for s in states if s.state == "Closed" and s.rev > first_active.rev]
    if states[-1].state == "Closed":
        result.cycle_end = states[-1].at
        if len(closes) > 1:
            result.flags.add("reopened")
    else:
        result.cycle_end = max(as_of, result.cycle_start)
        result.flags.add("open")
        if closes:
            result.flags.add("reopened")
    return True


# ---------------------------------------------------------------------------
# Topic state machine
# ---------------------------------------------------------------------------


@dataclass
class _ChangeState:
    exists: bool = False
    wip: bool = False
    approved: bool = False
    merged: bool = False
    abandoned: bool = False


@dataclass
class _TopicState:
    changes: dict[int, _ChangeState] = field(default_factory=dict)
    author_turn: bool = False
    ci_result: str | None = None

    def live(self) -> list[_ChangeState]:
        return [c for c in self.changes.values() if c.exists and not c.abandoned]


@dataclass(frozen=True)
class _BuildEvent:
    at: int
    build: Build
    kind: str  # finished; queued and started only matter as intervals


def _apply(state: _TopicState, event: ChangeEvent | _BuildEvent, inferred: set[int]) -> None:
    if isinstance(event, _BuildEvent):
        if event.build.result == "fail":
            state.ci_result = "fail"
            state.author_turn = True
        elif event.build.result == "pass":
            state.ci_result = "pass"
        return  # infra and canceled ask nothing of the author
    change = state.changes.setdefault(event.change, _ChangeState())
    if event.kind == "patchset":
        change.exists = True
        change.wip = event.wip
        if not event.keeps_votes:
            change.approved = False
        state.ci_result = None
        state.author_turn = False
        if not event.wip:
            inferred.add(event.at)  # coding ended by a non-WIP upload, not Ready-for-review
    elif event.kind == "ready":
        change.exists = True
        change.wip = False
        state.author_turn = False
        inferred.discard(event.at)
    elif event.kind == "wip":
        change.wip = True
    elif event.kind == "vote":
        if event.value == 2:
            change.approved = True
        elif event.value is not None and event.value < 0:
            change.approved = False
            state.author_turn = True
    elif event.kind == "merged":
        change.merged = True
    elif event.kind == "abandoned":
        change.abandoned = True


def _build_phase(builds: tuple[Build, ...], t: int) -> str | None:
    for build in builds:
        if (
            build.started_at is not None
            and build.started_at <= t
            and (build.finished_at is None or t < build.finished_at)
        ):
            return "ci_run"
    for build in builds:
        start = build.started_at if build.started_at is not None else build.finished_at
        if build.queued_at <= t and (start is None or t < start):
            return "ci_queue"
    return None


def _label(state: _TopicState, builds: tuple[Build, ...], t: int, had_gate: bool, ado: str) -> str:
    live = state.live()
    if live and all(c.merged for c in live):
        if ado == "Active":  # reopened after merge: new code is being written
            return "rework" if had_gate else "coding"
        return "close_lag"
    pending = [c for c in live if not c.merged]
    if not pending or state.author_turn or any(c.wip for c in pending):
        return "rework" if had_gate else "coding"
    phase = _build_phase(builds, t)
    if phase:
        return phase
    approved = all(c.approved for c in pending)
    if not approved:
        return "review_wait"
    if state.ci_result == "pass":
        return "submit_wait"
    return "unattributed"  # approved, but no build queued or passed yet


def _critical_topic(topics: list[Topic], end: int) -> Topic:
    def last_activity(topic: Topic) -> int:
        times = [e.at for c in topic.changes for e in c.events if e.at <= end]
        times += [b.finished_at or b.queued_at for b in topic.builds if b.queued_at <= end]
        merges = [
            e.at for c in topic.changes for e in c.events if e.kind == "merged" and e.at <= end
        ]
        return max(merges) if merges else max(times, default=0)

    return max(topics, key=last_activity)


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


def _merge_intervals(labels: list[tuple[int, int, str, int, tuple[str, ...]]]) -> list[Span]:
    spans: list[Span] = []
    for start, end, kind, rnd, flags in labels:
        if end <= start:
            continue
        if spans and spans[-1].kind == kind and spans[-1].round == rnd and spans[-1].end == start:
            last = spans[-1]
            spans[-1] = Span(
                kind, rnd, last.start, end, tuple(sorted(set(last.flags) | set(flags)))
            )
        else:
            spans.append(Span(kind, rnd, start, end, flags))
    return spans


def _ado_override(item: WorkItem, t: int) -> str | None:
    state = _ado_state_at(item, t)
    if state == "Closed":
        return "closed_interim"
    if state == "Proposed":
        return "parked"
    return None


def _ado_only(item: WorkItem, result: Result) -> list[Span]:
    start, end = result.cycle_start, result.cycle_end
    cuts = sorted({start, end} | {s.at for s in item.states if start < s.at < end})
    labels = []
    rnd, had_gate = 1, False
    for a, b in zip(cuts, cuts[1:], strict=False):
        state = _ado_state_at(item, a)
        kind = _ado_override(item, a)
        if kind is None:
            if state == "Resolved":
                kind, had_gate = "gate", True
            else:
                if had_gate and labels and labels[-1][2] in ("gate", "closed_interim"):
                    rnd += 1
                kind = "rework" if had_gate else "coding"
        labels.append((a, b, kind, rnd, ()))
    return _merge_intervals(labels)


def _with_topic(item: WorkItem, topic: Topic, result: Result) -> list[Span]:
    start, end = result.cycle_start, result.cycle_end
    events: list[ChangeEvent | _BuildEvent] = [e for c in topic.changes for e in c.events]
    events += [
        _BuildEvent(b.finished_at, b, "finished") for b in topic.builds if b.finished_at is not None
    ]
    events.sort(key=lambda e: e.at)
    cuts = {start, end} | {s.at for s in item.states if start < s.at < end}
    cuts |= {e.at for e in events if start < e.at < end}
    for b in topic.builds:
        cuts |= {
            t
            for t in (b.queued_at, b.started_at, b.finished_at)
            if t is not None and start < t < end
        }
    cuts_sorted = sorted(cuts)

    state, inferred = _TopicState(), set()
    i, rnd, had_gate = 0, 1, False
    prev_kind: str | None = None
    labels = []
    mismatch = 0
    for a, b in zip(cuts_sorted, cuts_sorted[1:], strict=False):
        while i < len(events) and events[i].at <= a:
            _apply(state, events[i], inferred)
            i += 1
        kind = _ado_override(item, a)
        flags: tuple[str, ...] = ()
        if kind is None:
            ado = _ado_state_at(item, a)
            kind = _label(state, topic.builds, a, had_gate, ado)
            if kind in AUTHOR_KINDS and prev_kind in ROUND_BREAKERS:
                rnd += 1
            if kind in GATE_KINDS:
                had_gate = True
                if a in inferred:
                    flags = ("inferred_patchset",)
            if (kind in AUTHOR_KINDS and ado == "Resolved") or (
                kind in GATE_KINDS and ado == "Active"
            ):
                mismatch += b - a
        labels.append((a, b, kind, rnd, flags))
        prev_kind = kind
    result.metrics["ado_mismatch_s"] = mismatch
    first_event = min((e.at for e in events), default=None)
    if first_event is not None and first_event < start:
        result.metrics["activity_before_active_s"] = start - first_event
        result.flags.add("activity_before_active")
    spans = _merge_intervals(labels)
    # The inferred flag marks the gate span that the non-WIP upload opened; move it to
    # the rework span that ended there, which is the one whose end is a guess.
    fixed: list[Span] = []
    for span in spans:
        if "inferred_patchset" in span.flags and fixed and fixed[-1].kind in AUTHOR_KINDS:
            prev = fixed.pop()
            fixed.append(Span(prev.kind, prev.round, prev.start, prev.end, ("inferred_patchset",)))
            span = Span(span.kind, span.round, span.start, span.end, ())
        fixed.append(span)
    return fixed


def compute_item(item: WorkItem, data: Input) -> Result:
    result = Result(item)
    if not _bounds(item, data.as_of, result):
        return result
    result.topics = data.topics_of(item.id)
    if not result.topics:
        result.flags.add("ado_only")
        result.spans = _ado_only(item, result)
    else:
        topic = _critical_topic(result.topics, result.cycle_end)
        result.critical_topic = topic.id
        if len(result.topics) > 1:
            result.flags.add("multi_topic")
        if len(topic.wi_ids) > 1:
            result.flags.add("shared_topic")
        result.spans = _with_topic(item, topic, result)
    result.metrics["rounds"] = max((s.round for s in result.spans), default=0)
    validate(result)
    return result


def compute(data: Input) -> list[Result]:
    return [
        compute_item(item, data) for item in sorted(data.work_items.values(), key=lambda w: w.id)
    ]


class PartitionError(AssertionError):
    """Spans do not account for the cycle exactly once."""


def validate(result: Result) -> None:
    """Conservation check: spans are contiguous, positive and cover the whole cycle."""
    if result.cycle_start is None:
        if result.spans:
            raise PartitionError(f"work item {result.item.id}: spans without a cycle")
        return
    if result.cycle_start == result.cycle_end:
        if result.spans:
            raise PartitionError(f"work item {result.item.id}: spans in an empty cycle")
        return
    cursor = result.cycle_start
    for span in result.spans:
        if span.start != cursor or span.end <= span.start or span.kind not in SPAN_KINDS:
            raise PartitionError(f"work item {result.item.id}: bad span {span} at {cursor}")
        cursor = span.end
    if cursor != result.cycle_end:
        raise PartitionError(
            f"work item {result.item.id}: spans end at {cursor}, cycle at {result.cycle_end}"
        )
