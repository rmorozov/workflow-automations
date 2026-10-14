"""Versioned raw snapshot of fetched work items, shared by ado-graph and ado-triage.

A snapshot keeps what ADO returned (fields and relations as plain JSON data) plus
the facts needed to replay it offline: the saved query's result IDs (``roots``),
the filters that decided which items were fetched, and the IDs ADO did not return.
Each tool converts a snapshot to and from its own model with an adapter; this
module only validates and stores the shared format and makes no requests.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "workflow-automations/ado-snapshot"
VERSION = 1


class SnapshotError(ValueError):
    """A snapshot file or object that does not follow the schema."""


@dataclass
class RawSnapshot:
    """Fetched work items as plain data.

    ``items``: fully fetched work items in fetch order,
    ``{"id": int, "fields": dict, "relations": [{"rel": str, "url": str | None}]}``.
    ``roots``: IDs the saved query returned, in query order.
    ``linked``: partial fields of items fetched only for labels (titles, AreaPaths).
    ``filters``: the producer's scope filters as lists of values; any non-empty
    list means ``items`` is not the complete set of reachable work items.
    ``options``: other producer settings needed to replay the run.
    ``omitted``: requested IDs that ADO did not return.
    ``source``: ``base_url``, ``project`` and ``query_id`` of the run.
    """

    producer: str
    source: dict[str, str]
    roots: list[int]
    items: list[dict[str, Any]]
    linked: dict[int, dict[str, Any]] = field(default_factory=dict)
    filters: dict[str, list[str]] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    omitted: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    captured_at: str = ""

    @property
    def is_filtered(self) -> bool:
        return any(self.filters.values())

    @property
    def item_ids(self) -> list[int]:
        return [item["id"] for item in self.items]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "version": VERSION,
            "producer": self.producer,
            "captured_at": self.captured_at,
            "source": dict(self.source),
            "filters": {name: list(values) for name, values in self.filters.items()},
            "options": dict(self.options),
            "roots": list(self.roots),
            "omitted": list(self.omitted),
            "warnings": list(self.warnings),
            "items": [_plain_item(item) for item in self.items],
            # JSON object keys are strings; from_json converts them back.
            "linked": {str(wi_id): dict(fields) for wi_id, fields in self.linked.items()},
        }

    @classmethod
    def from_json(cls, data: Any) -> RawSnapshot:
        """Validate a decoded snapshot document and build the object."""
        doc = _mapping(data, "snapshot")
        if doc.get("schema") != SCHEMA:
            raise SnapshotError(f"not an ADO snapshot: schema is {doc.get('schema')!r}")
        version = doc.get("version")
        if type(version) is not int:
            raise SnapshotError(f"snapshot version must be an integer, got {version!r}")
        if version != VERSION:
            raise SnapshotError(
                f"unsupported snapshot version {version}; this tool reads {VERSION}"
            )
        source = _mapping(doc.get("source", {}), "source")
        snapshot = cls(
            producer=_string(doc.get("producer"), "producer"),
            captured_at=_string(doc.get("captured_at", ""), "captured_at"),
            source={key: _string(value, f"source.{key}") for key, value in source.items()},
            filters={
                name: [_string(v, f"filters.{name}[]") for v in _list(values, f"filters.{name}")]
                for name, values in _mapping(doc.get("filters", {}), "filters").items()
            },
            options=dict(_mapping(doc.get("options", {}), "options")),
            roots=_ids(doc.get("roots"), "roots"),
            omitted=_ids(doc.get("omitted", []), "omitted"),
            warnings=[_string(w, "warnings[]") for w in _list(doc.get("warnings", []), "warnings")],
            items=[_item(raw, i) for i, raw in enumerate(_list(doc.get("items"), "items"))],
            linked={
                _key_id(key): dict(_mapping(fields, f"linked[{key}]"))
                for key, fields in _mapping(doc.get("linked", {}), "linked").items()
            },
        )
        seen: set[int] = set()
        for wi_id in snapshot.item_ids:
            if wi_id in seen:
                raise SnapshotError(f"work item {wi_id} appears twice in items")
            seen.add(wi_id)
        return snapshot


def utc_timestamp() -> str:
    """Current time as an ISO 8601 UTC string, for ``captured_at``."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def save_snapshot(snapshot: RawSnapshot, path: Path, overwrite: bool = False) -> None:
    """Write ``snapshot`` as UTF-8 JSON through a sibling temp file.

    An existing ``path`` is replaced only with ``overwrite``; a failed write leaves
    it unchanged. Field values must already be JSON data, as ADO returns them.
    """
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists; pass --overwrite to replace it")
    try:
        text = json.dumps(snapshot.to_json(), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"snapshot is not JSON data: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=path.suffix, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_snapshot(path: Path) -> RawSnapshot:
    """Read and validate a snapshot file; ``SnapshotError`` if it is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"{path} is not a JSON snapshot: {exc}") from exc
    return RawSnapshot.from_json(data)


def _plain_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "fields": dict(item["fields"]),
        "relations": [{"rel": r["rel"], "url": r["url"]} for r in item["relations"]],
    }


def _item(raw: Any, index: int) -> dict[str, Any]:
    where = f"items[{index}]"
    item = _mapping(raw, where)
    relations = []
    for j, rel in enumerate(_list(item.get("relations", []), f"{where}.relations")):
        rel = _mapping(rel, f"{where}.relations[{j}]")
        url = rel.get("url")
        if url is not None and not isinstance(url, str):
            raise SnapshotError(f"{where}.relations[{j}].url must be a string or null")
        relations.append(
            {"rel": _string(rel.get("rel"), f"{where}.relations[{j}].rel"), "url": url}
        )
    return {
        "id": _id(item.get("id"), f"{where}.id"),
        "fields": dict(_mapping(item.get("fields", {}), f"{where}.fields")),
        "relations": relations,
    }


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotError(f"{where} must be an object")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise SnapshotError(f"{where} must be a list")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise SnapshotError(f"{where} must be a string")
    return value


def _id(value: Any, where: str) -> int:
    # bool is an int subclass; True is not a work-item ID.
    if type(value) is not int:
        raise SnapshotError(f"{where} must be an integer work-item ID, got {value!r}")
    return value


def _ids(value: Any, where: str) -> list[int]:
    return [_id(v, f"{where}[]") for v in _list(value, where)]


def _key_id(key: str) -> int:
    if not (key.isascii() and key.isdigit()):
        raise SnapshotError(f"linked key {key!r} is not a work-item ID")
    return int(key)
