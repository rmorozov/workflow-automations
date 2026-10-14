"""Saved-query result IDs and work-item batch chunking.

The result types describe the azure-devops SDK's ``WorkItemQueryResult`` shape as
both tools read it. Nothing here decides what a missing work item means: ado-graph
asks the batch API to omit it, ado-triage fails on a missing core item.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol

# ADO limit on work items per get-work-items or batch request
ADO_BATCH_LIMIT = 200


class WorkItemReference(Protocol):
    id: int


class WorkItemLink(Protocol):
    source: WorkItemReference | None
    target: WorkItemReference | None


class QueryResult(Protocol):
    work_items: Sequence[WorkItemReference] | None
    work_item_relations: Sequence[WorkItemLink] | None


def flat_result_ids(result: QueryResult) -> list[int]:
    """IDs of a flat query's items, in query order."""
    return [ref.id for ref in result.work_items or []]


def link_result_ids(result: QueryResult) -> list[int]:
    """Each endpoint of a tree or one-hop query once, in first-seen order.

    Root rows of a tree result have no source and are skipped on that end.
    """
    seen: set[int] = set()
    ids: list[int] = []
    for link in result.work_item_relations or []:
        for ref in (link.source, link.target):
            if ref is not None and ref.id not in seen:
                seen.add(ref.id)
                ids.append(ref.id)
    return ids


def query_result_ids(result: QueryResult) -> list[int]:
    """Flat items and link endpoints together, each once in first-seen order."""
    return list(dict.fromkeys([*flat_result_ids(result), *link_result_ids(result)]))


def chunks(ids: Sequence[int], size: int = ADO_BATCH_LIMIT) -> Iterator[list[int]]:
    """Split ``ids`` into consecutive lists of at most ``size``."""
    if size < 1:
        raise ValueError(f"chunk size must be at least 1, got {size}")
    for start in range(0, len(ids), size):
        yield list(ids[start : start + size])
