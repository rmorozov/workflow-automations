"""Normalized input for cycle-time: work items, Gerrit topics and CI builds.

The input is one JSON object in the shape that the normalized layer will hold once
ingest exists (see docs/specs/cycle-time.md#input-file). Today it is written by hand
for synthetic scenarios. Times are ISO 8601 with an explicit zone and are kept as
integer UTC seconds. No person names or accounts appear anywhere in the model.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "workflow-automations/cycle-time-input"
VERSION = 1

STATES = ("Proposed", "Active", "Resolved", "Closed")
CHANGE_EVENTS = ("patchset", "ready", "wip", "vote", "merged", "abandoned")
BUILD_RESULTS = ("pass", "fail", "infra", "canceled")


class InputError(ValueError):
    """The input file is not a valid cycle-time input."""


@dataclass(frozen=True)
class StateEntry:
    rev: int
    state: str
    at: int


@dataclass(frozen=True)
class WorkItem:
    id: int
    type: str
    team: str
    title: str
    states: tuple[StateEntry, ...]


@dataclass(frozen=True)
class ChangeEvent:
    at: int
    kind: str
    change: int
    patchset: int | None = None
    wip: bool = False
    value: int | None = None
    keeps_votes: bool = False
    reviewer: str | None = None


@dataclass(frozen=True)
class Change:
    number: int
    repo: str
    events: tuple[ChangeEvent, ...]


@dataclass(frozen=True)
class Build:
    id: int
    topic: str
    queued_at: int
    started_at: int | None
    finished_at: int | None
    result: str | None
    # change number -> patchset number the build tested; None when the source lacks it
    tested: dict[int, int] | None = None


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    wi_ids: tuple[int, ...]
    changes: tuple[Change, ...]
    builds: tuple[Build, ...] = ()


@dataclass
class Input:
    as_of: int
    work_items: dict[int, WorkItem]
    topics: dict[str, Topic]
    warnings: list[str] = field(default_factory=list)
    # Observation cutoff per work item: the as_of of the file it came from.
    cutoffs: dict[int, int] = field(default_factory=dict)

    def cutoff(self, wi_id: int) -> int:
        return self.cutoffs.get(wi_id, self.as_of)

    def topics_of(self, wi_id: int) -> list[Topic]:
        return [t for t in self.topics.values() if wi_id in t.wi_ids]


def parse_time(value: Any, where: str) -> int:
    if not isinstance(value, str):
        raise InputError(f"{where}: time must be an ISO 8601 string")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise InputError(f"{where}: {value!r} is not ISO 8601") from exc
    if parsed.tzinfo is None:
        raise InputError(f"{where}: {value!r} has no time zone")
    return int(parsed.timestamp())


def format_time(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{where}: must be an integer")
    return value


def _opt_time(raw: dict, key: str, where: str) -> int | None:
    return None if raw.get(key) is None else parse_time(raw[key], f"{where}.{key}")


def _obj(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise InputError(f"{where}: must be an object")
    return raw


def _items(raw: dict, key: str, where: str) -> list:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputError(f"{where}.{key}: must be a list")
    return value


def _work_item(raw: Any, where: str) -> WorkItem:
    _obj(raw, where)
    wi_id = _int(raw.get("id"), f"{where}.id")
    states = []
    for i, entry in enumerate(_items(raw, "states", where)):
        at = f"{where}.states[{i}]"
        _obj(entry, at)
        if entry.get("state") not in STATES:
            raise InputError(f"{at}.state: must be one of {', '.join(STATES)}")
        states.append(
            StateEntry(
                _int(entry.get("rev"), f"{at}.rev"),
                entry["state"],
                parse_time(entry.get("at"), f"{at}.at"),
            )
        )
    if not states:
        raise InputError(f"{where}.states: needs at least one entry")
    states.sort(key=lambda s: s.rev)  # ADO revision order, not timestamps
    if len({s.rev for s in states}) != len(states):
        raise InputError(f"{where}.states: revision numbers repeat")
    return WorkItem(
        id=wi_id,
        type=str(raw.get("type", "Task")),
        team=str(raw.get("team", "")),
        title=str(raw.get("title", "")),
        states=tuple(states),
    )


def _change(raw: Any, where: str) -> Change:
    _obj(raw, where)
    number = _int(raw.get("number"), f"{where}.number")
    events = []
    for i, event in enumerate(_items(raw, "events", where)):
        at = f"{where}.events[{i}]"
        _obj(event, at)
        reviewer = event.get("reviewer")
        if reviewer is not None and not isinstance(reviewer, str):
            raise InputError(f"{at}.reviewer: must be a string (a pseudonymous key)")
        kind = event.get("kind")
        if kind not in CHANGE_EVENTS:
            raise InputError(f"{at}.kind: must be one of {', '.join(CHANGE_EVENTS)}")
        value = event.get("value")
        if kind == "vote":
            value = _int(value, f"{at}.value")
            if not -2 <= value <= 2:
                raise InputError(f"{at}.value: Code-Review votes are -2..2")
        events.append(
            ChangeEvent(
                at=parse_time(event.get("at"), f"{at}.at"),
                kind=kind,
                change=number,
                patchset=None
                if event.get("patchset") is None
                else _int(event["patchset"], f"{at}.patchset"),
                wip=bool(event.get("wip", False)),
                value=value,
                keeps_votes=bool(event.get("keeps_votes", False)),
                reviewer=reviewer,
            )
        )
    events.sort(key=lambda e: e.at)  # stable: same-second events keep file order
    return Change(number, str(raw.get("repo", "")), tuple(events))


def _tested(raw: Any, where: str) -> dict[int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise InputError(f"{where}: must map change numbers to patchset numbers")
    tested = {}
    for key, value in raw.items():
        if not str(key).isdecimal():
            raise InputError(f"{where}: {key!r} is not a change number")
        tested[int(key)] = _int(value, f"{where}.{key}")
    return tested


def _build(raw: Any, where: str, topic: str) -> Build:
    _obj(raw, where)
    result = raw.get("result")
    if result is not None and result not in BUILD_RESULTS:
        raise InputError(f"{where}.result: must be one of {', '.join(BUILD_RESULTS)}")
    build = Build(
        id=_int(raw.get("id"), f"{where}.id"),
        topic=topic,
        queued_at=parse_time(raw.get("queued_at"), f"{where}.queued_at"),
        started_at=_opt_time(raw, "started_at", where),
        finished_at=_opt_time(raw, "finished_at", where),
        result=result,
        tested=_tested(raw.get("tested"), f"{where}.tested"),
    )
    times = [t for t in (build.queued_at, build.started_at, build.finished_at) if t is not None]
    if times != sorted(times):
        raise InputError(f"{where}: queued_at <= started_at <= finished_at must hold")
    if build.finished_at is not None and result is None:
        raise InputError(f"{where}.result: a finished build needs a result")
    return build


def load(data: dict) -> Input:
    """Validate one input object; refuses another schema or version."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise InputError(f"schema must be {SCHEMA!r}")
    if data.get("version") != VERSION:
        raise InputError(f"version must be {VERSION}")
    as_of = parse_time(data.get("as_of"), "as_of")
    work_items: dict[int, WorkItem] = {}
    for i, raw in enumerate(_items(data, "work_items", "input")):
        item = _work_item(raw, f"work_items[{i}]")
        if item.id in work_items:
            raise InputError(f"work_items[{i}].id: {item.id} is listed twice")
        work_items[item.id] = item
    topics: dict[str, Topic] = {}
    warnings: list[str] = []
    for i, raw in enumerate(_items(data, "topics", "input")):
        where = f"topics[{i}]"
        _obj(raw, where)
        topic_id = raw.get("id")
        if not isinstance(topic_id, str) or not topic_id:
            raise InputError(f"{where}.id: must be a non-empty string")
        if topic_id in topics:
            raise InputError(f"{where}.id: {topic_id!r} is listed twice")
        wi_ids = tuple(_int(w, f"{where}.wi_ids") for w in _items(raw, "wi_ids", where))
        for wi_id in wi_ids:
            if wi_id not in work_items:
                warnings.append(
                    f"topic {topic_id!r} links work item {wi_id}, which is not in the input"
                )
        changes = tuple(
            _change(c, f"{where}.changes[{j}]") for j, c in enumerate(_items(raw, "changes", where))
        )
        builds = tuple(
            _build(b, f"{where}.builds[{j}]", topic_id)
            for j, b in enumerate(_items(raw, "builds", where))
        )
        topics[topic_id] = Topic(topic_id, str(raw.get("name", topic_id)), wi_ids, changes, builds)
    return Input(as_of, work_items, topics, warnings, {wi_id: as_of for wi_id in work_items})


def merge(inputs: list[Input]) -> Input:
    """Combine several input files; IDs must not collide.

    Each work item keeps the cutoff of its own file, so adding a later snapshot never
    extends an open item's last span. ``as_of`` is the latest cutoff.
    """
    merged = Input(max(i.as_of for i in inputs), {}, {})
    for part in inputs:
        for wi_id, item in part.work_items.items():
            if wi_id in merged.work_items:
                raise InputError(f"work item {wi_id} appears in more than one input")
            merged.work_items[wi_id] = item
            merged.cutoffs[wi_id] = part.cutoff(wi_id)
        for topic_id, topic in part.topics.items():
            if topic_id in merged.topics:
                raise InputError(f"topic {topic_id!r} appears in more than one input")
            merged.topics[topic_id] = topic
        merged.warnings.extend(part.warnings)
    return merged


def read(path: Path) -> Input:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON ({exc})") from exc
    try:
        return load(data)
    except InputError as exc:
        raise InputError(f"{path}: {exc}") from exc
