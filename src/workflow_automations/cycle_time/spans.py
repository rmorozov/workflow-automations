"""Split each work item's cycle into spans that account for every second.

ADO bounds the cycle: it starts at the first Active entry and ends at the final
Closed entry (or at ``as_of`` while the item is open). Inside the cycle, the
critical topic's Gerrit and CI events decide what the work item is waiting on at
each moment, and ADO states are only cross-checked. The rules are in
docs/specs/cycle-time.md#span-rules.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from workflow_automations.cycle_time.model import (
    REVIEW_LABEL,
    SECOND,
    Build,
    ChangeEvent,
    Input,
    LabelRules,
    Topic,
    WorkItem,
)

AUTHOR_KINDS = ("coding", "rework")
GATE_KINDS = (
    "review_wait",
    "ci_queue",
    "ci_run",
    "ci_retry",
    "ci_failed",
    "submit_wait",
    "unattributed",
    "gate",
)
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
    cutoff: int = 0  # observation cutoff of the item's input file

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
    # Code-Review votes by pseudonymous reviewer key; None is the change-level label.
    votes: dict[str | None, int] = field(default_factory=dict)
    merged: bool = False
    merged_at: int | None = None
    abandoned: bool = False
    patchset: int | None = None
    # CI label votes on the current patchset: label -> (value, time of the vote).
    ci: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return 2 in self.votes.values() and -2 not in self.votes.values()

    @property
    def rejected(self) -> bool:  # an outstanding negative vote
        return any(v < 0 for v in self.votes.values())


@dataclass
class _TopicState:
    changes: dict[int, _ChangeState] = field(default_factory=dict)
    # Finished pass/fail builds, oldest first. The verdict is re-derived from them at
    # each step rather than cached, so any later change of revision or composition
    # (an upload, a new change, an abandon, a merge of another patchset) retires it.
    finished: list[Build] = field(default_factory=list)
    # Last time the topic's composition changed: an upload, a change first seen through
    # any event, or an abandon.
    composition_at: int | None = None
    _pending: dict[int, _ChangeState] | None = None
    # Label mode: required CI labels per change, and how each negative CI vote was
    # resolved, keyed by (change, label, vote time): "rework", "retry" or "unresolved".
    required: dict[int, frozenset[str]] | None = None
    resolution: dict[tuple[int, str, int], str] = field(default_factory=dict)

    def touched(self) -> None:
        self._pending = None
        self.finished = [b for b in self.finished if not self.retired(b)]

    def _failures(self) -> list[tuple[int, str, int]]:
        """Negative required CI votes on pending changes: (change, label, vote time)."""
        assert self.required is not None
        return [
            (n, label, c.ci[label][1])
            for n, c in self.pending().items()
            for label in sorted(self.required.get(n, ()))
            if label in c.ci and c.ci[label][0] < 0
        ]

    def _label_verdict(self) -> str | None:
        assert self.required is not None
        if self._failures():
            return "fail"
        for n, c in self.pending().items():
            if any(c.ci.get(label, (0, 0))[0] <= 0 for label in self.required.get(n, ())):
                return None
        return "pass"

    def failure(self) -> str:
        """How the current CI failure was resolved: by new code, a re-run, or not yet.

        A −1 cannot tell a broken change from a network failure, so the next step
        decides: a new patchset means the author had work to do, a re-run on the same
        patchset means nobody changed the code. Build verdicts always count as rework.
        """
        if self.required is None:
            return "rework"
        found = {self.resolution.get(f, "unresolved") for f in self._failures()}
        for outcome in ("rework", "unresolved"):
            if outcome in found:
                return outcome
        return "retry"

    def record(self, build: Build) -> None:
        """Keep a finished build, dropping the ones it makes redundant.

        ``tests_current`` depends only on the tested map and the queue time, and a later
        queue time only widens it. So an earlier build with the same map (or both
        without one) and an earlier or equal queue time is current only when this one
        is too, and this one comes first in ``verdict``. Repeated builds of the same
        revision set therefore keep one entry, while a verdict for another map stays
        available in case an abandon makes it current again.
        """
        self.finished = [
            b
            for b in self.finished
            if not (b.tested == build.tested and b.queued_at <= build.queued_at)
        ]
        self.finished.append(build)

    def live(self) -> list[_ChangeState]:
        return [c for c in self.changes.values() if c.exists and not c.abandoned]

    def pending(self) -> dict[int, _ChangeState]:
        if self._pending is None:
            self._pending = {
                n: c
                for n, c in self.changes.items()
                if c.exists and not c.abandoned and not c.merged
            }
        return self._pending

    def retired(self, build: Build) -> bool:
        """Stale for good: a tested change moved on or was abandoned, a sibling merged
        after the queue time without being tested, or (without ``tested``) the
        composition changed after the queue time."""
        if build.tested is None:
            return self.composition_at is not None and build.queued_at <= self.composition_at
        for number, patchset in build.tested.items():
            change = self.changes.get(number)
            if change is not None and change.exists:
                if change.abandoned or change.patchset != patchset:
                    return True
        return any(
            c.merged and n not in build.tested and c.merged_at >= build.queued_at
            for n, c in self.changes.items()
            if c.exists and not c.abandoned
        )

    def tests_current(self, build: Build) -> bool:
        """Whether the build tested the topic's current revision and composition.

        With ``tested`` (change → patchset): every tested change must still be live at
        the tested patchset, every pending change must be tested, and a change merged
        at or after the build's queue time must have been tested too. Only a change merged
        strictly before the queue time was certainly in the base and may be left out.
        Without ``tested``, the build must have been queued strictly after the last
        upload, new change or abandon; a tie at the same instant is ambiguous and the
        build counts as stale.
        """
        pending = self.pending()
        if not pending:
            return False
        if build.tested is None:
            return self.composition_at is None or build.queued_at > self.composition_at
        for number, patchset in build.tested.items():
            change = self.changes.get(number)
            if change is None or not change.exists or change.abandoned:
                return False
            if change.patchset != patchset:
                return False
        for number, change in self.changes.items():
            if not change.exists or change.abandoned or number in build.tested:
                continue
            if not change.merged or change.merged_at >= build.queued_at:
                return False  # a merge at the queue instant may not be in the base
        return True

    def verdict(self) -> str | None:
        if self.required is not None:
            return self._label_verdict()
        for build in reversed(self.finished):
            if self.tests_current(build):
                return build.result
        return None


@dataclass(frozen=True)
class _BuildEvent:
    at: int
    build: Build


def _apply(state: _TopicState, event: ChangeEvent | _BuildEvent, inferred: set[int]) -> None:
    if isinstance(event, _BuildEvent):
        if state.required is not None:
            return  # labels carry the verdict; builds only explain queue and run time
        if event.build.result in ("pass", "fail") and not state.retired(event.build):
            state.record(event.build)
        return  # infra and canceled ask nothing of the author
    change = state.changes.setdefault(event.change, _ChangeState())
    if not change.exists:
        # Any event proves the change exists, as in _critical_topic: a vote, a WIP
        # flag or a merge can be the first one observed. A new member changes the
        # topic's composition.
        change.exists = True
        state.composition_at = event.at
    if event.kind == "patchset":
        change.wip = event.wip
        change.patchset = event.patchset
        change.ci = {}  # CI labels are reset on every new patchset
        change.votes = {k: v for k, v in change.votes.items() if v > 0} if event.keeps_votes else {}
        state.composition_at = event.at
        if not event.wip:
            inferred.add(event.at)  # coding ended by a non-WIP upload, not Ready-for-review
    elif event.kind == "ready":
        change.wip = False
        # The author has answered: objections are back with the reviewers.
        change.votes = {k: v for k, v in change.votes.items() if v >= 0}
        inferred.discard(event.at)
    elif event.kind == "wip":
        change.wip = True
    elif event.kind == "vote" and event.label != REVIEW_LABEL:
        # A CI label counts only for the patchset it was cast on.
        if event.patchset is None or event.patchset == change.patchset:
            change.ci[event.label] = (event.value, event.at)
    elif event.kind == "vote" and event.patchset not in (None, change.patchset):
        pass  # a vote (or withdrawal) for another patchset doesn't touch the current one
    elif event.kind == "vote":
        if event.reviewer is None:
            change.votes = {None: event.value}  # change-level effective label
        else:
            change.votes.pop(None, None)
            change.votes[event.reviewer] = event.value
    elif event.kind == "merged":
        change.merged = True
        change.merged_at = event.at
    elif event.kind == "abandoned":
        change.abandoned = True
        state.composition_at = event.at
    state.touched()


def _build_phase(state: _TopicState, active: list[Build], t: int) -> str | None:
    current = [b for b in active if state.tests_current(b)]
    for build in current:
        if build.started_at is not None and build.started_at <= t:
            return "ci_run"
    if current:
        return "ci_queue"
    return None


def _label(state: _TopicState, active: list[Build], t: int, had_gate: bool, reopened: bool) -> str:
    author = "rework" if had_gate else "coding"
    live = state.live()
    pending = list(state.pending().values())
    if live and not pending:
        # Everything is merged. Only a reopen (Active again after a Closed) is new
        # author work; otherwise the item waits for its bookkeeping close.
        return author if reopened else "close_lag"
    if not pending:
        return author
    verdict = state.verdict()
    failure = state.failure() if verdict == "fail" else None
    if failure == "rework":
        return author
    if any(c.wip or c.rejected for c in pending):
        return author
    phase = _build_phase(state, active, t)
    if phase:
        return phase
    if failure == "retry":
        return "ci_retry"  # a re-run follows without new code: likely infrastructure
    if failure == "unresolved":
        return "ci_failed"  # nothing has answered the failure yet
    if not all(c.approved for c in pending):
        return "review_wait"
    if verdict == "pass":
        return "submit_wait"
    return "unattributed"  # approved, but no build of this revision queued or passed yet


def _critical_topic(topics: list[Topic], end: int) -> Topic:
    """The topic that became complete last, else the one with the latest activity.

    A topic is complete when every live change (seen and not abandoned) is merged.
    It becomes complete at the event that makes it so: the last merge, or an
    abandon that removes the last pending change. Only events up to ``end`` count.
    """

    def key(topic: Topic) -> tuple[bool, int]:
        events = sorted(
            ((e.at, e.kind, c.number) for c in topic.changes for e in c.events if e.at <= end),
            key=lambda x: x[0],
        )
        seen: set[int] = set()
        gone: set[int] = set()
        merged: set[int] = set()
        completed_at: int | None = None
        for at, kind, number in events:
            if kind == "abandoned":
                gone.add(number)
            else:
                seen.add(number)
                if kind == "merged":
                    merged.add(number)
            live = seen - gone
            complete = bool(live) and live <= merged
            if complete and completed_at is None:
                completed_at = at
            elif not complete:
                completed_at = None
        if completed_at is not None:
            return (True, completed_at)
        times = [at for at, _, _ in events]
        for b in topic.builds:
            times += [t for t in (b.queued_at, b.started_at, b.finished_at) if t is not None]
        return (False, max((t for t in times if t <= end), default=0))

    return max(topics, key=key)


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


def _reopened_at(item: WorkItem, t: int) -> bool:
    """ADO is Active at ``t`` and a Closed entry came before it: the item was reopened."""
    seen_closed = False
    state = None
    for entry in item.states:
        if entry.at > t:
            break
        if entry.state == "Closed":
            seen_closed = True
        state = entry.state
    return state == "Active" and seen_closed


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
                if labels and labels[-1][2] in ("gate", "closed_interim"):
                    rnd += 1
                    had_gate = True
                kind = "rework" if had_gate else "coding"
        labels.append((a, b, kind, rnd, ()))
    return _merge_intervals(labels)


def _resolutions(topic: Topic, end: int) -> dict[tuple[int, str, int], str]:
    """Classify each negative CI vote by what answered it first, up to ``end``.

    A new patchset of that change means ``rework``. A build of the topic queued later,
    or a new vote on the same label and patchset, without new code means ``retry``.
    A tie goes to ``rework``, because an upload triggers its own build. Nothing by
    ``end`` (the work item's cycle end) means ``unresolved``, so later activity on a
    shared topic can't rewrite a closed item. One reverse pass per change keeps the
    next upload and the next vote per label and patchset; queues are bisected.
    """
    queues = sorted(b.queued_at for b in topic.builds if b.queued_at <= end)
    out: dict[tuple[int, str, int], str] = {}
    for change in topic.changes:
        events = [e for e in change.events if e.at <= end]
        on: list[int | None] = []  # patchset each event applies to
        patchset = None
        for event in events:
            if event.kind == "patchset":
                patchset = event.patchset
            on.append(event.patchset if event.patchset is not None else patchset)
        next_upload: int | None = None
        next_vote: dict[tuple[str, int | None], int] = {}
        for event, ps in zip(reversed(events), reversed(on), strict=True):
            if event.kind == "vote" and event.label != REVIEW_LABEL and event.value < 0:
                k = bisect.bisect_right(queues, event.at)
                reruns = [t for t in (next_vote.get((event.label, ps)),) if t is not None]
                if k < len(queues):
                    reruns.append(queues[k])
                first_rerun = min(reruns, default=None)
                if next_upload is not None and (first_rerun is None or next_upload <= first_rerun):
                    outcome = "rework"
                elif first_rerun is not None:
                    outcome = "retry"
                else:
                    outcome = "unresolved"
                out[(change.number, event.label, event.at)] = outcome
            if event.kind == "patchset":
                next_upload = event.at
            elif event.kind == "vote" and event.label != REVIEW_LABEL:
                next_vote[(event.label, ps)] = event.at
    return out


def _with_topic(
    item: WorkItem, topic: Topic, result: Result, rules: LabelRules | None = None
) -> list[Span]:
    start, end = result.cycle_start, result.cycle_end
    events: list[ChangeEvent | _BuildEvent] = [e for c in topic.changes for e in c.events]
    events += [_BuildEvent(b.finished_at, b) for b in topic.builds if b.finished_at is not None]
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

    # Builds sweep: `active` holds builds queued and not yet finished at the current cut.
    by_queue = sorted(topic.builds, key=lambda b: b.queued_at)
    active: list[Build] = []
    q = 0

    state, inferred = _TopicState(), set()
    if rules is not None:
        state.required = {c.number: rules.required(c.repo, c.branch) for c in topic.changes}
        state.resolution = _resolutions(topic, end)
    i, rnd, had_gate = 0, 1, False
    prev_kind: str | None = None
    labels = []
    mismatch = 0
    for a, b in zip(cuts_sorted, cuts_sorted[1:], strict=False):
        while i < len(events) and events[i].at <= a:
            _apply(state, events[i], inferred)
            i += 1
        while q < len(by_queue) and by_queue[q].queued_at <= a:
            active.append(by_queue[q])
            q += 1
        active = [b for b in active if b.finished_at is None or a < b.finished_at]
        kind = _ado_override(item, a)
        flags: tuple[str, ...] = ()
        if kind is None:
            ado = _ado_state_at(item, a)
            kind = _label(state, active, a, had_gate, _reopened_at(item, a))
            if kind in AUTHOR_KINDS and prev_kind in ROUND_BREAKERS:
                rnd += 1
            if kind in GATE_KINDS:
                had_gate = True
                if a in inferred and prev_kind in AUTHOR_KINDS:
                    flags = ("inferred_patchset",)
            if (kind in AUTHOR_KINDS and ado == "Resolved") or (
                kind in GATE_KINDS and ado == "Active"
            ):
                mismatch += b - a
        if kind in ("close_lag", "closed_interim"):
            had_gate = True  # author time after a close is rework
        labels.append((a, b, kind, rnd, flags))
        prev_kind = kind
    result.metrics["ado_mismatch_s"] = mismatch // SECOND
    activity = [e.at for c in topic.changes for e in c.events]
    for b in topic.builds:
        activity += [t for t in (b.queued_at, b.started_at, b.finished_at) if t is not None]
    first_event = min(activity, default=None)
    if first_event is not None and first_event < start:
        result.metrics["activity_before_active_s"] = (start - first_event) // SECOND
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
    result = Result(item, cutoff=data.cutoff(item.id))
    if not _bounds(item, data.cutoff(item.id), result):
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
        if any(len(set(t.wi_ids)) > 1 for t in result.topics):
            result.flags.add("shared_topic")
        result.spans = _with_topic(item, topic, result, data.label_rules(item.id))
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
