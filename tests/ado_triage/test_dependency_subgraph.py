"""Triage critical paths come from the shared ADO dependency subgraph (SH-007)."""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("pandas")

from workflow_automations import ado_triage as tool  # noqa: E402
from workflow_automations.ado_common import relations as R  # noqa: E402
from workflow_automations.ado_common.dependency import (  # noqa: E402
    DependencySubgraph,
    dependency_edge,
)

# One set of ADO links as (emitting item, reference name, other end).
LINKS = [
    (1, R.BLOCKS_FORWARD, 2),  # 1 blocks 2
    (3, R.BLOCKS_REVERSE, 2),  # 2 blocks 3, stored on the blocked item
    (2, R.BLOCKS_FORWARD, 3),  # the same link from the other end
    (4, R.DEPENDENCY_REVERSE, 3),  # 4 depends on 3
    (4, R.DEPENDENCY_FORWARD, 5),  # 5 depends on 4
    (1, R.HIERARCHY_FORWARD, 9),  # parent-child: not a dependency
    (6, R.AFFECTS_FORWARD, 1),  # impact: not a dependency
    (7, R.BLOCKS_FORWARD, 7),  # self-link
]
EXPECTED_EDGES = {(1, 2), (2, 3), (3, 4), (4, 5)}


def labelled(schema: R.RelationSchema) -> list[tuple[int, str, int]]:
    return [(s, schema.label(ref), t) for s, ref, t in LINKS if schema.label(ref)]


def test_dependency_edge_normalizes_direction_and_skips_other_families():
    assert dependency_edge(1, R.BLOCKS_FORWARD, 2) == (1, 2)
    assert dependency_edge(2, R.BLOCKS_REVERSE, 1) == (1, 2)
    assert dependency_edge(1, R.DEPENDENCY_FORWARD, 2) == (1, 2)
    assert dependency_edge(2, R.DEPENDENCY_REVERSE, 1) == (1, 2)
    assert dependency_edge(1, R.HIERARCHY_FORWARD, 2) is None
    assert dependency_edge(1, R.IMPLEMENTS_FORWARD, 2) is None
    assert dependency_edge(1, None, 2) is None
    assert dependency_edge(1, "Custom.Link-Forward", 2) is None
    assert dependency_edge(3, R.BLOCKS_FORWARD, 3) is None


def test_both_tools_labels_give_the_same_subgraph():
    from_refs = DependencySubgraph.from_references(LINKS)
    assert from_refs.edges == EXPECTED_EDGES
    assert DependencySubgraph.from_labels(labelled(R.TRIAGE), R.TRIAGE) == from_refs
    assert DependencySubgraph.from_labels(labelled(R.GRAPH), R.GRAPH) == from_refs


def test_labels_another_schema_does_not_read_are_skipped():
    rows = [(1, "Blocks", 2), (2, "Predecessor->Successor", 3), (3, "Unknown", 4)]
    assert DependencySubgraph.from_labels(rows, R.TRIAGE).edges == {(1, 2)}


def test_components_are_emitted_downstream_first():
    graph = DependencySubgraph(frozenset({(1, 2), (2, 1), (2, 3), (4, 1)}))
    assert graph.components() == [[3], [1, 2], [4]]


def test_triage_chains_are_the_shared_subgraphs_chains():
    items = {i: {"id": i, "fields": {}, "relations": []} for i in range(1, 6)}
    for src, ref, dst in LINKS:
        if src in items:
            items[src]["relations"].append(
                {"rel": ref, "url": f"https://x/_apis/wit/workItems/{dst}"}
            )
    snap = tool.Snapshot(items=list(items.values()))
    cfg = tool.Settings(as_of=datetime(2026, 1, 1), base_url="https://x")
    rel = tool.build_relations(snap, cfg)

    shared = DependencySubgraph.from_references(LINKS)
    assert tool.dependency_subgraph(rel) == shared
    assert tool.blocker_chains(rel) == shared.downstream_chains()
    assert tool.blocker_chains(rel)[1] == [2, 3, 4, 5]
