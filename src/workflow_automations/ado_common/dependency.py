"""A normalized dependency subgraph that critical paths are read from.

ado-triage reads its blocker chains from it. ado-graph's labels normalize to the
same edges through the ``GRAPH`` schema, though ado-graph still builds its own
subgraph (which also drops edges to items outside its nodes file and keeps
self-links).

A dependency relation is any registered link of the ``dependency`` or ``blocks``
family (``LINK_TYPES``). Normalized, it is one directed edge
``(upstream, downstream)``: the downstream item waits on the upstream one. ADO
reports a link from each end, so a forward reference name reads
``source -> target`` and a reverse one is mirrored to ``target -> source``. The
same link recorded on both ends is then one edge, whichever tool labelled it,
because labels are translated back to reference names through the tool's
``RelationSchema``.

Hierarchy and impact links are not dependencies: a parent or an affecting item is
not something an item waits for. Self-links are dropped.

Standard library only, so ado-triage can use it without the graph extras.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from workflow_automations.ado_common.relations import FORWARD, LINK_TYPES, RelationSchema

DEPENDENCY_FAMILIES = frozenset({"dependency", "blocks"})

Edge = tuple[int, int]


def dependency_edge(source: int, reference: str | None, target: int) -> Edge | None:
    """``(upstream, downstream)`` for a dependency link, or ``None`` for any other link."""
    link = LINK_TYPES.get(reference or "")
    if link is None or link.family not in DEPENDENCY_FAMILIES or source == target:
        return None
    return (source, target) if link.direction == FORWARD else (target, source)


@dataclass(frozen=True)
class DependencySubgraph:
    """Directed dependency edges, upstream to downstream, with critical-path queries."""

    edges: frozenset[Edge]
    _successors: dict[int, list[int]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        successors: dict[int, list[int]] = {}
        for upstream, downstream in sorted(self.edges):
            successors.setdefault(upstream, []).append(downstream)
            successors.setdefault(downstream, [])
        object.__setattr__(self, "_successors", successors)

    @classmethod
    def from_references(cls, links: Iterable[tuple[int, str | None, int]]) -> DependencySubgraph:
        """Subgraph of ``(source, reference name, target)`` links; non-dependencies skipped."""
        edges = {edge for src, ref, dst in links if (edge := dependency_edge(src, ref, dst))}
        return cls(frozenset(edges))

    @classmethod
    def from_labels(
        cls, links: Iterable[tuple[int, str, int]], schema: RelationSchema
    ) -> DependencySubgraph:
        """Subgraph of ``(source, label, target)`` rows labelled by ``schema``."""
        reference_of = {label: ref for ref, label in schema.labels.items()}
        if len(reference_of) != len(schema.labels):
            raise ValueError(f"{schema.name}: a label names more than one reference")
        return cls.from_references(
            (int(src), reference_of.get(label), int(dst)) for src, label, dst in links
        )

    @property
    def nodes(self) -> list[int]:
        return sorted(self._successors)

    def successors(self, node: int) -> list[int]:
        """Items waiting directly on ``node``, ascending."""
        return list(self._successors.get(node, ()))

    def components(self) -> list[list[int]]:
        """Strongly connected components, members ascending, downstream-first.

        Tarjan's algorithm, iterative: a component is emitted only after every
        component it reaches, so sinks come before sources.
        """
        graph = self._successors
        index: dict[int, int] = {}
        low: dict[int, int] = {}
        stack: list[int] = []
        on_stack: set[int] = set()
        components: list[list[int]] = []
        for root in sorted(graph):
            if root in index:
                continue
            work = [(root, iter(graph[root]))]
            index[root] = low[root] = len(index)
            stack.append(root)
            on_stack.add(root)
            while work:
                node, successors = work[-1]
                for nxt in successors:
                    if nxt not in index:
                        index[nxt] = low[nxt] = len(index)
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, iter(graph[nxt])))
                        break
                    if nxt in on_stack:
                        low[node] = min(low[node], index[nxt])
                else:
                    work.pop()
                    if work:
                        low[work[-1][0]] = min(low[work[-1][0]], low[node])
                    if low[node] == index[node]:
                        component = []
                        while True:
                            member = stack.pop()
                            on_stack.discard(member)
                            component.append(member)
                            if member == node:
                                break
                        components.append(sorted(component))
        return components

    def downstream_chains(self) -> dict[int, list[int]]:
        """Longest chain of items transitively waiting on each node, nearest first.

        A cycle is collapsed: its members are counted once, in ID order, and the
        node itself is left out of its own chain. Edges are visited in sorted
        order, so equally long chains resolve the same way on every run. Dynamic
        programming over the condensation: each component is visited once.
        """
        graph = self._successors
        component_of: dict[int, int] = {}
        members_of: list[list[int]] = []
        below: list[list[int]] = []  # per component: longest chain below it
        for number, members in enumerate(self.components()):
            for member in members:
                component_of[member] = number
            best: list[int] = []
            reached = {component_of[n] for m in members for n in graph[m]} - {number}
            for other in sorted(reached):
                candidate = members_of[other] + below[other]
                if len(candidate) > len(best):
                    best = candidate
            members_of.append(members)
            below.append(best)
        return {
            node: [m for m in members_of[component_of[node]] if m != node]
            + below[component_of[node]]
            for node in graph
        }
