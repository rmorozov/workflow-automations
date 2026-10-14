"""`analyze` mode: NetworkX graph analysis and bridge contraction."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import networkx as nx
import pandas as pd

from workflow_automations.ado_graph.report import write_analysis_report

log = logging.getLogger("ado_tool")


# ---------------------------------------------------------------------------
# GraphAnalyzer
# ---------------------------------------------------------------------------


DEFAULT_MAX_INDIRECT_EDGES = 1_000_000

# Step budgets.  Cycle enumeration can be exponential in a dense strongly connected
# component, so it stops at whichever budget is reached first and marks the result
# partial.  Exact betweenness costs one BFS per node (O(n·m)); above this many nodes
# it is estimated from a fixed-seed sample of source nodes instead.
DEFAULT_MAX_CYCLES = 1_000
DEFAULT_MAX_CYCLE_ROWS = 100_000
DEFAULT_CENTRALITY_SOURCES = 500
CENTRALITY_SEED = 0
CYCLE_PROGRESS_EVERY = 250


class ContractionBudgetExceeded(ValueError):
    """Bridge contraction would create more indirect edges than allowed."""


# Forward labels in report order, each with its relation family.  Reverse labels
# (REVERSE_LINK_MAP) are flipped to these, so one relation is one directed edge.
RELATION_FAMILY: dict[str, str] = {
    "Parent->Child": "hierarchy",
    "Blocks": "dependency",
    "Predecessor->Successor": "dependency",
    "Affects": "impact",
    "Implements": "impact",
}

# Named subgraphs a step can run on: `--steps cycles@dependency`.
SUBGRAPHS: dict[str, frozenset[str]] = {
    "all": frozenset(RELATION_FAMILY.values()),
    "hierarchy": frozenset({"hierarchy"}),
    "dependency": frozenset({"dependency"}),
    "impact": frozenset({"impact"}),
}

# Subgraph each step uses when none is named.  Downstream CR steps follow every
# relation family; prerequisites follow dependency relations only, since a
# parent or an affecting item is not something the CR waits for.
STEP_SUBGRAPH: dict[str, str] = {
    "cycles": "all",
    "orphans": "all",
    "no_cr_dep": "all",
    "has_cr_dep": "all",
    "longest_chain": "all",
    "scc": "all",
    "metrics": "all",
    "cr_scope_impact": "all",
    "cr_prereqs": "dependency",
    "cr_impact_unpacked": "all",
    "cr_prereqs_unpacked": "dependency",
}

PARALLEL_LABEL_SEPARATOR = " & "


def _longest_depth(dag: nx.DiGraph) -> dict:
    """
    Hops on the longest path from any source to each node of a DAG.

    Topological dynamic programming: a source has depth 0 and every other node is one
    more than its deepest predecessor, so a shortcut edge never lowers a depth.
    """
    depth: dict = {}
    for node in nx.topological_sort(dag):
        depth[node] = max((depth[p] + 1 for p in dag.predecessors(node)), default=0)
    return depth


def _edge_path(edge_data: dict) -> tuple[str, ...]:
    return edge_data.get("path") or (str(edge_data.get("link_type")),)


def _edge_label(edge_data: dict) -> str:
    """Link label for reports; indirect edges name their bridges and link types."""
    label = str(edge_data.get("link_type"))
    via = edge_data.get("via")
    if not via:
        return label
    return f"{label} via {'>'.join(map(str, via))} ({', '.join(_edge_path(edge_data))})"


class GraphAnalyzer:
    """
    Keeps every normalized relation in a MultiDiGraph and runs each analysis step
    on a DiGraph projection of an explicit subgraph (SUBGRAPHS, STEP_SUBGRAPH).

    A projection has one edge per ordered pair; parallel relation types on that
    pair are all kept in its `link_types` and joined in `link_type`.
    """

    DIRECTED_LINK_TYPES: set[str] = set(RELATION_FAMILY)

    # Map reverse link types to their forward counterparts and indicate that nodes must flip
    REVERSE_LINK_MAP: dict[str, str] = {
        "Child->Parent": "Parent->Child",
        "Affected By": "Affects",
        "Blocked By": "Blocks",
        "Successor->Predecessor": "Predecessor->Successor",
        "Implemented By": "Implements",
    }

    def __init__(
        self,
        df_nodes: pd.DataFrame,
        df_edges: pd.DataFrame,
        ado_url: str = "",
        effort_field: str = "",
        bridges: str = "contract",
        max_indirect_edges: int = DEFAULT_MAX_INDIRECT_EDGES,
        max_cycles: int = DEFAULT_MAX_CYCLES,
        max_cycle_rows: int = DEFAULT_MAX_CYCLE_ROWS,
        centrality_sources: int = DEFAULT_CENTRALITY_SOURCES,
    ) -> None:
        self.df_nodes = df_nodes
        self.df_edges = df_edges
        self.ado_url = ado_url.rstrip("/")
        self.effort_field = effort_field
        self.bridges = bridges
        self.max_indirect_edges = max_indirect_edges
        self.max_cycles = max_cycles
        self.max_cycle_rows = max_cycle_rows
        self.centrality_sources = centrality_sources
        self.relations: nx.MultiDiGraph = nx.MultiDiGraph()
        self._graphs: dict[str, nx.DiGraph] = {}
        self._bridge_ids: list = []
        self._build_relations()
        # Contracting the full graph first bounds every other projection: a subset
        # of relations never gives a bridge more neighbours.
        self.G: nx.DiGraph = self.graph("all")
        self.df_nodes = self.df_nodes[~self.df_nodes["WI_ID"].isin(self._bridge_ids)]
        self.df_nodes = self.df_nodes.reset_index(drop=True)

    def _wi_url(self, wi_id: int) -> str:
        return f"{self.ado_url}/_workitems/edit/{wi_id}" if self.ado_url else ""

    def _build_relations(self) -> None:
        valid_nodes = set(self.df_nodes["WI_ID"])

        for _, row in self.df_nodes.iterrows():
            self.relations.add_node(
                row["WI_ID"],
                WorkItemType=row.get("WorkItemType", ""),
                Title=row.get("Title", ""),
                State=row.get("State", ""),
                Roadmap_State=row.get("KL.Roadmap_State", ""),
                AreaPath=row.get("System.AreaPath", ""),
                IterationPath=row.get("System.IterationPath", ""),
            )
        if "In_Scope" in self.df_nodes.columns:
            in_scope = self.df_nodes["In_Scope"].fillna(True).astype(bool)
            self._bridge_ids = list(self.df_nodes.loc[~in_scope, "WI_ID"])

        unknown: dict[str, int] = {}
        phantom = 0
        for _, row in self.df_edges.iterrows():
            src, tgt, ltype = row["Source_ID"], row["Target_ID"], row["Link_Type"]
            if ltype in self.REVERSE_LINK_MAP:
                src, tgt, ltype = tgt, src, self.REVERSE_LINK_MAP[ltype]
            elif ltype not in self.DIRECTED_LINK_TYPES:
                unknown[str(ltype)] = unknown.get(str(ltype), 0) + 1
                continue
            if src not in valid_nodes or tgt not in valid_nodes:
                phantom += 1
            else:
                # The label is the key: the same relation reported from both ends
                # is one edge, while different relation types stay parallel edges.
                self.relations.add_edge(src, tgt, key=ltype, family=RELATION_FAMILY[ltype])

        for ltype, count in sorted(unknown.items()):
            log.warning("Ignored %d edge(s) with unknown link type %r.", count, ltype)
        log.info(
            "Graph built: %d nodes, %d normalized relations; skipped %d edge(s) with an "
            "absent endpoint.",
            self.relations.number_of_nodes(),
            self.relations.number_of_edges(),
            phantom,
        )

    def graph(self, subgraph: str = "all") -> nx.DiGraph:
        """DiGraph of the named subgraph's relation families, with bridges contracted."""
        if subgraph in self._graphs:
            return self._graphs[subgraph]
        if subgraph not in SUBGRAPHS:
            raise ValueError(f"unknown subgraph {subgraph!r}; choose one of {', '.join(SUBGRAPHS)}")
        families = SUBGRAPHS[subgraph]
        order = list(RELATION_FAMILY)
        labels: dict[tuple, list[str]] = {}
        for src, tgt, ltype in self.relations.edges(keys=True):
            if RELATION_FAMILY[ltype] in families:
                labels.setdefault((src, tgt), []).append(ltype)
        g = nx.DiGraph()
        g.add_nodes_from(self.relations.nodes(data=True))
        for (src, tgt), types in labels.items():
            link_types = tuple(sorted(types, key=order.index))
            g.add_edge(
                src,
                tgt,
                link_type=PARALLEL_LABEL_SEPARATOR.join(link_types),
                link_types=link_types,
            )
        self._contract_bridges(g, subgraph)
        self._graphs[subgraph] = g
        return g

    def _contract_bridges(self, g: nx.DiGraph, subgraph: str) -> None:
        """
        Replace out-of-scope bridge nodes (In_Scope == False) with indirect edges.

        For a bridge B with P -> B -> S, add P -> S labelled "Indirect", with the
        bridge IDs in its `via` attribute and the normalized link types along the
        way in `path`, unless P -> S already exists.  Chains of
        bridges collapse into one edge.  A cycle passing only through bridges back
        to the same node (P -> B -> P) is dropped rather than kept as a self-loop.
        Exports without an In_Scope column are treated as fully in scope.  Only
        relations of the subgraph's families are followed through a bridge.

        Each bridge costs predecessors × successors candidate pairs.  Before a
        bridge is contracted, the running total is checked against
        max_indirect_edges and ContractionBudgetExceeded is raised, so a dense
        bridge fails fast instead of materializing millions of edges.  With
        bridges="drop", bridges are removed without adding indirect edges.
        """
        if not self._bridge_ids:
            return
        added = 0
        work = 0
        for bridge in self._bridge_ids:
            if self.bridges == "drop":
                g.remove_node(bridge)
                continue
            work += g.in_degree(bridge) * g.out_degree(bridge)
            if work > self.max_indirect_edges:
                raise ContractionBudgetExceeded(
                    f"bridge contraction needs more than {self.max_indirect_edges:,} candidate "
                    f"indirect edges (reached {work:,} at bridge {bridge}); rerun with a larger "
                    "--max-indirect-edges or with --bridges drop"
                )
            for pred in list(g.predecessors(bridge)):
                for succ in list(g.successors(bridge)):
                    if pred == succ or pred == bridge or succ == bridge:
                        continue
                    if g.has_edge(pred, succ):
                        continue
                    into, out = g[pred][bridge], g[bridge][succ]
                    via = into.get("via", ()) + (bridge,) + out.get("via", ())
                    path = _edge_path(into) + _edge_path(out)
                    g.add_edge(
                        pred,
                        succ,
                        link_type="Indirect",
                        link_types=("Indirect",),
                        via=via,
                        path=path,
                    )
                    added += 1
            g.remove_node(bridge)
        log.info(
            "%s %d bridge node(s) in the %s subgraph; added %d indirect edge(s); "
            "%d in-scope nodes remain.",
            "Dropped" if self.bridges == "drop" else "Contracted",
            len(self._bridge_ids),
            subgraph,
            added,
            g.number_of_nodes(),
        )

    def _step_graph(self, step: str, subgraph: str | None) -> tuple[str, nx.DiGraph]:
        name = subgraph or STEP_SUBGRAPH[step]
        return name, self.graph(name)

    def _cr_nodes(self, g: nx.DiGraph) -> list:
        return [n for n, d in g.nodes(data=True) if d.get("WorkItemType") == "Change Request"]

    def _meta(self, wi_id: int, prefix: str = "") -> dict:
        """Helper: Returns standardized metadata columns for any Node."""
        d = self.G.nodes.get(wi_id, {})
        return {
            f"{prefix}Title": d.get("Title", ""),
            f"{prefix}WI_URL": self._wi_url(wi_id),
            f"{prefix}State": d.get("State", ""),
            f"{prefix}Roadmap_State": d.get("Roadmap_State", ""),
            f"{prefix}WorkItemType": d.get("WorkItemType", ""),
            f"{prefix}AreaPath": d.get("AreaPath", ""),
            f"{prefix}IterationPath": d.get("IterationPath", ""),
        }

    def _get_neighborhood_diagnostic(self, node_id: int) -> str:
        """Helper to generate a human-readable string of direct connections for debugging."""
        if node_id not in self.G:
            return ""

        diagnostics = []
        # Incoming edges (What points to this node)
        for pred in self.G.predecessors(node_id):
            edge_data = self.G.edges[pred, node_id]
            pred_type = self.G.nodes[pred].get("WorkItemType", "Unknown")
            diagnostics.append(f"<- [{_edge_label(edge_data)}] from {pred_type} ({pred})")

        # Outgoing edges (What this node points to)
        for succ in self.G.successors(node_id):
            edge_data = self.G.edges[node_id, succ]
            succ_type = self.G.nodes[succ].get("WorkItemType", "Unknown")
            diagnostics.append(f"-> [{_edge_label(edge_data)}] to {succ_type} ({succ})")

        return " | ".join(diagnostics) if diagnostics else "Isolated Node"

    def _nodes_subset(self, node_ids: list) -> pd.DataFrame:
        """Generates full data-dumps for subsets enriched with debugging metrics."""
        df = self.df_nodes[self.df_nodes["WI_ID"].isin(node_ids)].copy().reset_index(drop=True)
        if df.empty:
            return df

        # Inject the debugging metrics requested
        df["WI_URL"] = df["WI_ID"].apply(self._wi_url)
        df["Graph_Neighbors_Debug"] = df["WI_ID"].apply(self._get_neighborhood_diagnostic)

        # Shift structural validation metrics right next to the Title column for readability
        cols = list(df.columns)
        if "Title" in cols:
            title_idx = cols.index("Title")
            cols.insert(title_idx + 1, cols.pop(cols.index("WI_URL")))
            cols.insert(title_idx + 2, cols.pop(cols.index("Graph_Neighbors_Debug")))
            df = df[cols]
        else:
            wi_id_idx = cols.index("WI_ID")
            cols.insert(wi_id_idx + 1, cols.pop(cols.index("WI_URL")))
            cols.insert(wi_id_idx + 2, cols.pop(cols.index("Graph_Neighbors_Debug")))
            df = df[cols]

        return df

    def find_cycles(self, subgraph: str | None = None) -> pd.DataFrame:
        """
        Simple cycles, one row per member, enumerated within a budget.

        Enumeration stops after max_cycles cycles, or after the cycle that brings
        the member rows to max_cycle_rows or more.  `Cycles_Complete` on every row
        says whether every cycle was listed; when it is False, the `scc` step still
        names every node that lies on some cycle.
        """
        name, g = self._step_graph("cycles", subgraph)
        rows = []
        count = 0
        complete = True
        for cycle in nx.simple_cycles(g):
            if count >= self.max_cycles or len(rows) >= self.max_cycle_rows:
                complete = False
                break
            for node in cycle:
                row = {"Cycle_Index": count, "Node_ID": node}
                row.update(self._meta(node))
                rows.append(row)
            count += 1
            if count % CYCLE_PROGRESS_EVERY == 0:
                log.info("  … %d cycles, %d member rows so far", count, len(rows))
        for row in rows:
            row["Cycles_Complete"] = complete
        if complete:
            log.info("Cycles found in the %s subgraph: %d", name, count)
        else:
            log.warning(
                "Cycle enumeration in the %s subgraph stopped at its budget (%d cycles, "
                "%d member rows; limits %d cycles, %d rows); the cycles sheet is partial. "
                "The scc step lists every node on a cycle.",
                name,
                count,
                len(rows),
                self.max_cycles,
                self.max_cycle_rows,
            )
        columns = [
            "Cycle_Index",
            "Node_ID",
            "Title",
            "WI_URL",
            "State",
            "Roadmap_State",
            "WorkItemType",
            "AreaPath",
            "IterationPath",
            "Cycles_Complete",
        ]
        df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
        df.attrs["cycles_complete"] = complete
        return df

    def find_orphans(self, subgraph: str | None = None) -> pd.DataFrame:
        name, g = self._step_graph("orphans", subgraph)
        orphan_ids = [n for n in g.nodes if g.in_degree(n) == 0 and g.out_degree(n) == 0]
        log.info("Orphans in the %s subgraph: %d", name, len(orphan_ids))
        return self._nodes_subset(orphan_ids)

    def find_no_cr_dep(self, subgraph: str | None = None) -> pd.DataFrame:
        name, g = self._step_graph("no_cr_dep", subgraph)
        reachable = self._cr_downstream_set(g)
        no_dep = [n for n in g.nodes if n not in reachable]
        log.info("WIs without CR dependency in the %s subgraph: %d", name, len(no_dep))
        return self._nodes_subset(no_dep)

    def find_has_cr_dep(self, subgraph: str | None = None) -> pd.DataFrame:
        name, g = self._step_graph("has_cr_dep", subgraph)
        reachable = self._cr_downstream_set(g)
        has_dep = [n for n in g.nodes if n in reachable]
        log.info("WIs with CR dependency in the %s subgraph: %d", name, len(has_dep))
        return self._nodes_subset(has_dep)

    def find_longest_chain(self, subgraph: str | None = None) -> pd.DataFrame:
        name, g = self._step_graph("longest_chain", subgraph)
        columns = [
            "Chain_Length",
            "Chain_Rank",
            "SCC_Index",
            "WI_ID",
            "Title",
            "WI_URL",
            "State",
            "Roadmap_State",
            "WorkItemType",
            "AreaPath",
            "IterationPath",
        ]
        cond = nx.condensation(g)
        if len(cond) == 0:
            log.info("Longest chain in the %s subgraph (condensed DAG hops): 0", name)
            return pd.DataFrame(columns=columns)

        length = nx.dag_longest_path_length(cond)
        path_scc_indices = nx.dag_longest_path(cond)
        mapping: dict = cond.graph.get("mapping", {})
        scc_members: dict[int, list] = {}
        for node, scc_idx in mapping.items():
            scc_members.setdefault(scc_idx, []).append(node)
        rows = []
        for rank, scc_idx in enumerate(path_scc_indices):
            for wi_id in scc_members.get(scc_idx, []):
                row = {
                    "Chain_Length": length,
                    "Chain_Rank": rank,
                    "SCC_Index": scc_idx,
                    "WI_ID": wi_id,
                }
                row.update(self._meta(wi_id))
                rows.append(row)
        log.info("Longest chain in the %s subgraph (condensed DAG hops): %d", name, length)
        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=columns)
        df.attrs["longest_path_length"] = length
        return df

    def find_scc(self, subgraph: str | None = None) -> pd.DataFrame:
        name, g = self._step_graph("scc", subgraph)
        sccs = list(nx.strongly_connected_components(g))
        rows = []
        for i, scc in enumerate(sccs):
            for node in scc:
                row = {"SCC_Index": i, "WI_ID": node, "SCC_Size": len(scc)}
                row.update(self._meta(node))
                rows.append(row)
        log.info("SCCs found in the %s subgraph: %d", name, len(sccs))
        if not rows:
            return pd.DataFrame(
                columns=[
                    "SCC_Index",
                    "WI_ID",
                    "SCC_Size",
                    "Title",
                    "WI_URL",
                    "State",
                    "Roadmap_State",
                    "WorkItemType",
                    "AreaPath",
                    "IterationPath",
                ]
            )
        return pd.DataFrame(rows)

    def compute_metrics(self, subgraph: str | None = None) -> pd.DataFrame:
        name, g = self._step_graph("metrics", subgraph)
        bc, sources = self._betweenness(g, name)
        cond = nx.condensation(g)
        mapping: dict = cond.graph.get("mapping", {})
        scc_depth = _longest_depth(cond)
        rows = []
        for node in g.nodes:
            row = {
                "WI_ID": node,
                "In_Degree": g.in_degree(node),
                "Out_Degree": g.out_degree(node),
                "Betweenness_Centrality": round(bc.get(node, 0.0), 6),
                "Betweenness_Sources": sources,
                "Dependency_Depth": scc_depth.get(mapping.get(node, -1), 0),
            }
            row.update(self._meta(node))
            rows.append(row)
        df = pd.DataFrame(rows)
        df.attrs["betweenness_sources"] = sources
        return df

    def _betweenness(self, g: nx.DiGraph, name: str) -> tuple[dict, int]:
        """
        Normalized betweenness and the number of source nodes it was computed from.

        Exact (every node a source) up to centrality_sources nodes; above that, a
        sample of that many sources chosen with a fixed seed, so repeat runs on the
        same graph give the same estimate.  The cost is one BFS per source.
        """
        n = g.number_of_nodes()
        if n <= self.centrality_sources:
            log.info("Betweenness in the %s subgraph: exact, %d sources", name, n)
            return nx.betweenness_centrality(g, normalized=True), n
        log.warning(
            "Betweenness in the %s subgraph: %d nodes is above the %d-source budget; "
            "estimating from %d sampled sources (seed %d).",
            name,
            n,
            self.centrality_sources,
            self.centrality_sources,
            CENTRALITY_SEED,
        )
        bc = nx.betweenness_centrality(
            g, k=self.centrality_sources, normalized=True, seed=CENTRALITY_SEED
        )
        return bc, self.centrality_sources

    def _cr_downstream_set(self, g: nx.DiGraph) -> set:
        """Change Requests and every node reachable from one (items that depend on a CR)."""
        reachable: set = set()
        for cr in self._cr_nodes(g):
            if cr not in reachable:
                reachable.add(cr)
                reachable.update(nx.descendants(g, cr))
        return reachable

    def analyze_cr_scope_and_impact(self, subgraph: str | None = None) -> pd.DataFrame:
        _name, g = self._step_graph("cr_scope_impact", subgraph)
        rows = []
        has_effort = self.effort_field in self.df_nodes.columns
        effort_label = self.effort_field.split(".")[-1] if self.effort_field else "Effort"

        for cr_id in self._cr_nodes(g):
            descendants = list(nx.descendants(g, cr_id))
            total_effort = 0.0
            if has_effort:
                subset = self.df_nodes[self.df_nodes["WI_ID"].isin([cr_id] + descendants)]
                total_effort = (
                    pd.to_numeric(subset[self.effort_field], errors="coerce").fillna(0).sum()
                )

            row = {
                "CR_ID": cr_id,
                "CR_Title": g.nodes[cr_id].get("Title", ""),
                f"Total_{effort_label}": total_effort,
                "Impacted_WI_Count": len(descendants),
                "Impacted_WI_IDs": ",".join(map(str, descendants)),
            }
            row.update(self._meta(cr_id, prefix="CR_"))
            rows.append(row)

        sort_col = f"Total_{effort_label}" if has_effort else "Impacted_WI_Count"
        if not rows:
            # The same leading columns as a non-empty result, each named once.
            return pd.DataFrame(
                columns=[
                    "CR_ID",
                    "CR_Title",
                    f"Total_{effort_label}",
                    "Impacted_WI_Count",
                    "Impacted_WI_IDs",
                ]
            )
        return pd.DataFrame(rows).sort_values(sort_col, ascending=False)

    def analyze_cr_prerequisites(self, subgraph: str | None = None) -> pd.DataFrame:
        _name, g = self._step_graph("cr_prereqs", subgraph)
        rows = []
        for cr_id in self._cr_nodes(g):
            prereqs = list(nx.ancestors(g, cr_id))
            row = {
                "CR_ID": cr_id,
                "CR_Title": g.nodes[cr_id].get("Title", ""),
                "Prerequisite_WI_Count": len(prereqs),
                "Prerequisite_WI_IDs": ",".join(map(str, prereqs)),
            }
            row.update(self._meta(cr_id, prefix="CR_"))
            rows.append(row)
        if not rows:
            return pd.DataFrame(
                columns=["CR_ID", "CR_Title", "Prerequisite_WI_Count", "Prerequisite_WI_IDs"]
            )
        return pd.DataFrame(rows).sort_values("Prerequisite_WI_Count", ascending=False)

    def analyze_cr_impact_unpacked(self, subgraph: str | None = None) -> pd.DataFrame:
        """Flat 1:1 view of CRs and every individual downstream impact WI."""
        _name, g = self._step_graph("cr_impact_unpacked", subgraph)
        rows = []
        for cr_id in self._cr_nodes(g):
            cr_meta = self._meta(cr_id, prefix="CR_")
            cr_title = g.nodes[cr_id].get("Title", "")
            for desc_id in nx.descendants(g, cr_id):
                row = {"CR_ID": cr_id, "CR_Title": cr_title}
                row.update(cr_meta)
                row.update(
                    {
                        "Impacted_WI_ID": desc_id,
                        "Impacted_Title": g.nodes[desc_id].get("Title", ""),
                    }
                )
                row.update(self._meta(desc_id, prefix="Impacted_"))
                rows.append(row)
        log.info("Generated unpacked impact view (%d rows).", len(rows))
        if not rows:
            return pd.DataFrame(columns=["CR_ID", "CR_Title", "Impacted_WI_ID", "Impacted_Title"])
        return pd.DataFrame(rows)

    def analyze_cr_prereqs_unpacked(self, subgraph: str | None = None) -> pd.DataFrame:
        """Flat 1:1 view of CRs and every individual upstream prerequisite WI."""
        _name, g = self._step_graph("cr_prereqs_unpacked", subgraph)
        rows = []
        for cr_id in self._cr_nodes(g):
            cr_meta = self._meta(cr_id, prefix="CR_")
            cr_title = g.nodes[cr_id].get("Title", "")
            for asc_id in nx.ancestors(g, cr_id):
                row = {"CR_ID": cr_id, "CR_Title": cr_title}
                row.update(cr_meta)
                row.update(
                    {"Prereq_WI_ID": asc_id, "Prereq_Title": g.nodes[asc_id].get("Title", "")}
                )
                row.update(self._meta(asc_id, prefix="Prereq_"))
                rows.append(row)
        log.info("Generated unpacked prerequisites view (%d rows).", len(rows))
        if not rows:
            return pd.DataFrame(columns=["CR_ID", "CR_Title", "Prereq_WI_ID", "Prereq_Title"])
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Analyze pipeline
# ---------------------------------------------------------------------------

ALL_STEPS = (
    "cycles",
    "orphans",
    "no_cr_dep",
    "has_cr_dep",
    "longest_chain",
    "scc",
    "metrics",
    "cr_scope_impact",
    "cr_prereqs",
    "cr_impact_unpacked",
    "cr_prereqs_unpacked",
)


def parse_steps(raw: str) -> list[tuple[str, str, str | None]]:
    """
    Parse --steps into (sheet name, step, subgraph) triples.

    `all` adds every step on its default subgraph.  A step may name a subgraph as
    `step@subgraph`; its sheet is then named `step@subgraph` (lowercased, spaces
    removed).  Empty tokens are ignored; unknown steps and subgraphs warn and are
    skipped.
    """
    parsed = []
    for token in raw.split(","):
        step, _, subgraph = token.strip().lower().partition("@")
        step, subgraph = step.strip(), subgraph.strip()
        if not step:
            continue
        if step == "all" and not subgraph:
            parsed.extend((name, name, None) for name in ALL_STEPS)
        elif step not in ALL_STEPS:
            log.warning("Unknown analysis step '%s' – skipping.", token.strip())
        elif subgraph and subgraph not in SUBGRAPHS:
            log.warning(
                "Unknown subgraph in '%s' (choose %s) – skipping.",
                token.strip(),
                ", ".join(SUBGRAPHS),
            )
        else:
            parsed.append((f"{step}@{subgraph}" if subgraph else step, step, subgraph or None))
    return parsed


def run_analyze(args: argparse.Namespace) -> None:
    """Entry point for `analyze` mode."""
    df_nodes = pd.read_parquet(args.nodes_file)
    df_edges = pd.read_parquet(args.edges_file)
    log.info("Loaded %d nodes, %d edges.", len(df_nodes), len(df_edges))

    steps_requested = parse_steps(args.steps)

    try:
        analyzer = GraphAnalyzer(
            df_nodes,
            df_edges,
            args.ado_url,
            args.effort_field,
            bridges=args.bridges,
            max_indirect_edges=args.max_indirect_edges,
            max_cycles=args.max_cycles,
            max_cycle_rows=args.max_cycle_rows,
            centrality_sources=args.centrality_sources,
        )
    except ContractionBudgetExceeded as exc:
        # Raised before any output is created.
        raise SystemExit(f"error: {exc}") from None
    step_fn_map = {
        "cycles": analyzer.find_cycles,
        "orphans": analyzer.find_orphans,
        "no_cr_dep": analyzer.find_no_cr_dep,
        "has_cr_dep": analyzer.find_has_cr_dep,
        "longest_chain": analyzer.find_longest_chain,
        "scc": analyzer.find_scc,
        "metrics": analyzer.compute_metrics,
        "cr_scope_impact": analyzer.analyze_cr_scope_and_impact,
        "cr_prereqs": analyzer.analyze_cr_prerequisites,
        "cr_impact_unpacked": analyzer.analyze_cr_impact_unpacked,
        "cr_prereqs_unpacked": analyzer.analyze_cr_prereqs_unpacked,
    }

    results: dict[str, pd.DataFrame] = {}
    total = len(steps_requested)
    for position, (sheet, step, subgraph) in enumerate(steps_requested, start=1):
        log.info("Running step %d/%d: %s …", position, total, sheet)
        started = time.monotonic()
        try:
            results[sheet] = step_fn_map[step](subgraph=subgraph)
        except KeyboardInterrupt:
            # Nothing has been written yet; the report is published only at the end.
            log.error(
                "Interrupted during step %s after %.1fs; no report written.",
                sheet,
                time.monotonic() - started,
            )
            raise SystemExit(130) from None
        log.info("Step %s finished in %.1fs.", sheet, time.monotonic() - started)

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    write_analysis_report(results, output_path)
