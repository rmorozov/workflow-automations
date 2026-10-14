"""`analyze` mode: NetworkX graph analysis and bridge contraction."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import networkx as nx
import pandas as pd

from workflow_automations.ado_graph.report import write_analysis_report

log = logging.getLogger("ado_tool")


# ---------------------------------------------------------------------------
# GraphAnalyzer
# ---------------------------------------------------------------------------


DEFAULT_MAX_INDIRECT_EDGES = 1_000_000


class ContractionBudgetExceeded(ValueError):
    """Bridge contraction would create more indirect edges than allowed."""


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
    """Builds a NetworkX DiGraph and runs the requested analysis steps."""

    # Only forward-direction labels so each logical relationship maps to
    # exactly ONE directed edge (avoids contradictory A→B and B→A pairs).
    DIRECTED_LINK_TYPES: set[str] = {
        "Parent->Child",
        "Affects",
        "Blocks",
        "Predecessor->Successor",
        "Implements",
    }

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
    ) -> None:
        self.df_nodes = df_nodes
        self.df_edges = df_edges
        self.ado_url = ado_url.rstrip("/")
        self.effort_field = effort_field
        self.bridges = bridges
        self.max_indirect_edges = max_indirect_edges
        self.G: nx.DiGraph = nx.DiGraph()
        self._build_graph()

    def _wi_url(self, wi_id: int) -> str:
        return f"{self.ado_url}/_workitems/edit/{wi_id}" if self.ado_url else ""

    def _build_graph(self) -> None:
        valid_nodes = set(self.df_nodes["WI_ID"])

        for _, row in self.df_nodes.iterrows():
            self.G.add_node(
                row["WI_ID"],
                WorkItemType=row.get("WorkItemType", ""),
                Title=row.get("Title", ""),
                State=row.get("State", ""),
                Roadmap_State=row.get("KL.Roadmap_State", ""),
                AreaPath=row.get("System.AreaPath", ""),
                IterationPath=row.get("System.IterationPath", ""),
            )

        # Normalize and ingest edges
        edge_count = 0
        for _, row in self.df_edges.iterrows():
            src, tgt, ltype = row["Source_ID"], row["Target_ID"], row["Link_Type"]

            # Skip phantom nodes not present in nodes dataframe
            if src not in valid_nodes or tgt not in valid_nodes:
                continue

            if ltype in self.DIRECTED_LINK_TYPES:
                # Regular forward edge
                self.G.add_edge(src, tgt, link_type=ltype)
                edge_count += 1
            elif ltype in self.REVERSE_LINK_MAP:
                # FIX: Flip the edge direction and normalize the label
                normalized_type = self.REVERSE_LINK_MAP[ltype]
                self.G.add_edge(tgt, src, link_type=normalized_type)
                edge_count += 1

        log.info(
            "Graph built: %d nodes, %d normalized edges.", self.G.number_of_nodes(), edge_count
        )
        self._contract_bridges()

    def _contract_bridges(self) -> None:
        """
        Replace out-of-scope bridge nodes (In_Scope == False) with indirect edges.

        For a bridge B with P -> B -> S, add P -> S labelled "Indirect", with the
        bridge IDs in its `via` attribute and the normalized link types along the
        way in `path`, unless P -> S already exists.  Chains of
        bridges collapse into one edge.  A cycle passing only through bridges back
        to the same node (P -> B -> P) is dropped rather than kept as a self-loop.
        Exports without an In_Scope column are treated as fully in scope.

        Each bridge costs predecessors × successors candidate pairs.  Before a
        bridge is contracted, the running total is checked against
        max_indirect_edges and ContractionBudgetExceeded is raised, so a dense
        bridge fails fast instead of materializing millions of edges.  With
        bridges="drop", bridges are removed without adding indirect edges.
        """
        if "In_Scope" not in self.df_nodes.columns:
            return
        in_scope = self.df_nodes["In_Scope"].fillna(True).astype(bool)
        bridges = list(self.df_nodes.loc[~in_scope, "WI_ID"])
        added = 0
        work = 0
        for bridge in bridges:
            if self.bridges == "drop":
                self.G.remove_node(bridge)
                continue
            work += self.G.in_degree(bridge) * self.G.out_degree(bridge)
            if work > self.max_indirect_edges:
                raise ContractionBudgetExceeded(
                    f"bridge contraction needs more than {self.max_indirect_edges:,} candidate "
                    f"indirect edges (reached {work:,} at bridge {bridge}); rerun with a larger "
                    "--max-indirect-edges or with --bridges drop"
                )
            for pred in list(self.G.predecessors(bridge)):
                for succ in list(self.G.successors(bridge)):
                    if pred == succ or pred == bridge or succ == bridge:
                        continue
                    if self.G.has_edge(pred, succ):
                        continue
                    into, out = self.G[pred][bridge], self.G[bridge][succ]
                    via = into.get("via", ()) + (bridge,) + out.get("via", ())
                    path = _edge_path(into) + _edge_path(out)
                    self.G.add_edge(pred, succ, link_type="Indirect", via=via, path=path)
                    added += 1
            self.G.remove_node(bridge)
        self.df_nodes = self.df_nodes[in_scope].reset_index(drop=True)
        log.info(
            "%s %d bridge node(s); added %d indirect edge(s); %d in-scope nodes remain.",
            "Dropped" if self.bridges == "drop" else "Contracted",
            len(bridges),
            added,
            self.G.number_of_nodes(),
        )

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

    def find_cycles(self) -> pd.DataFrame:
        cycles = list(nx.simple_cycles(self.G))
        rows = []
        for i, c in enumerate(cycles):
            for node in c:
                row = {"Cycle_Index": i, "Node_ID": node}
                row.update(self._meta(node))
                rows.append(row)
        log.info("Cycles found: %d", len(cycles))
        if not rows:
            return pd.DataFrame(
                columns=[
                    "Cycle_Index",
                    "Node_ID",
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

    def find_orphans(self) -> pd.DataFrame:
        orphan_ids = [
            n for n in self.G.nodes if self.G.in_degree(n) == 0 and self.G.out_degree(n) == 0
        ]
        log.info("Orphans found: %d", len(orphan_ids))
        return self._nodes_subset(orphan_ids)

    def find_no_cr_dep(self) -> pd.DataFrame:
        reachable = self._cr_reachable_set()
        no_dep = [n for n in self.G.nodes if n not in reachable]
        log.info("WIs without CR dependency: %d", len(no_dep))
        return self._nodes_subset(no_dep)

    def find_has_cr_dep(self) -> pd.DataFrame:
        reachable = self._cr_reachable_set()
        has_dep = [n for n in self.G.nodes if n in reachable]
        log.info("WIs with CR dependency: %d", len(has_dep))
        return self._nodes_subset(has_dep)

    def find_longest_chain(self) -> pd.DataFrame:
        cond = nx.condensation(self.G)
        if len(cond) == 0:
            log.info("Longest chain (condensed DAG hops): 0")
            return pd.DataFrame(
                columns=[
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
            )

        length = nx.dag_longest_path_length(cond)
        path_scc_indices = nx.dag_longest_path(cond)
        mapping: dict = cond.graph.get("mapping", {})
        scc_members: dict[int, list] = {}
        for node, scc_idx in mapping.items():
            scc_members.setdefault(scc_idx, []).append(node)
        rows = []
        for rank, scc_idx in enumerate(path_scc_indices):
            for wi_id in scc_members.get(scc_idx, []):
                row = {"Chain_Rank": rank, "SCC_Index": scc_idx, "WI_ID": wi_id}
                row.update(self._meta(wi_id))
                rows.append(row)
        log.info("Longest chain (condensed DAG hops): %d", length)
        df = (
            pd.DataFrame(rows)
            if rows
            else pd.DataFrame(
                columns=[
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
            )
        )
        df.attrs["longest_path_length"] = length
        return df

    def find_scc(self) -> pd.DataFrame:
        sccs = list(nx.strongly_connected_components(self.G))
        rows = []
        for i, scc in enumerate(sccs):
            for node in scc:
                row = {"SCC_Index": i, "WI_ID": node, "SCC_Size": len(scc)}
                row.update(self._meta(node))
                rows.append(row)
        log.info("SCCs found: %d", len(sccs))
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

    def compute_metrics(self) -> pd.DataFrame:
        bc = nx.betweenness_centrality(self.G, normalized=True)
        cond = nx.condensation(self.G)
        mapping: dict = cond.graph.get("mapping", {})
        sources = [n for n in cond.nodes if cond.in_degree(n) == 0]
        scc_depth: dict[int, int] = {n: 0 for n in cond.nodes}
        for src in sources:
            for scc_idx, dist in nx.single_source_shortest_path_length(cond, src).items():
                if dist > scc_depth.get(scc_idx, 0):
                    scc_depth[scc_idx] = dist
        rows = []
        for node in self.G.nodes:
            row = {
                "WI_ID": node,
                "In_Degree": self.G.in_degree(node),
                "Out_Degree": self.G.out_degree(node),
                "Betweenness_Centrality": round(bc.get(node, 0.0), 6),
                "Dependency_Depth": scc_depth.get(mapping.get(node, -1), 0),
            }
            row.update(self._meta(node))
            rows.append(row)
        return pd.DataFrame(rows)

    def _cr_reachable_set(self) -> set:
        cr_nodes = {
            n for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        }
        if not cr_nodes:
            return set()

        rev = self.G.reverse(copy=False)
        reachable: set = set(cr_nodes)
        for cr in cr_nodes:
            reachable.update(nx.descendants(rev, cr))
        return reachable

    def analyze_cr_scope_and_impact(self) -> pd.DataFrame:
        cr_nodes = [
            (n, d) for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        ]
        rows = []
        has_effort = self.effort_field in self.df_nodes.columns
        effort_label = self.effort_field.split(".")[-1] if self.effort_field else "Effort"

        for cr_id, data in cr_nodes:
            descendants = list(nx.descendants(self.G, cr_id))
            total_effort = 0.0
            if has_effort:
                subset = self.df_nodes[self.df_nodes["WI_ID"].isin([cr_id] + descendants)]
                total_effort = (
                    pd.to_numeric(subset[self.effort_field], errors="coerce").fillna(0).sum()
                )

            row = {
                "CR_ID": cr_id,
                "CR_Title": data.get("Title", ""),
                f"Total_{effort_label}": total_effort,
                "Impacted_WI_Count": len(descendants),
                "Impacted_WI_IDs": ",".join(map(str, descendants)),
            }
            row.update(self._meta(cr_id, prefix="CR_"))
            rows.append(row)

        sort_col = f"Total_{effort_label}" if has_effort else "Impacted_WI_Count"
        if not rows:
            return pd.DataFrame(
                columns=["CR_ID", "CR_Title", sort_col, "Impacted_WI_Count", "Impacted_WI_IDs"]
            )
        return pd.DataFrame(rows).sort_values(sort_col, ascending=False)

    def analyze_cr_prerequisites(self) -> pd.DataFrame:
        cr_nodes = [
            (n, d) for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        ]
        rows = []
        for cr_id, data in cr_nodes:
            prereqs = list(nx.ancestors(self.G, cr_id))
            row = {
                "CR_ID": cr_id,
                "CR_Title": data.get("Title", ""),
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

    def analyze_cr_impact_unpacked(self) -> pd.DataFrame:
        """Flat 1:1 view of CRs and every individual downstream impact WI."""
        cr_nodes = [
            n for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        ]
        rows = []
        for cr_id in cr_nodes:
            cr_meta = self._meta(cr_id, prefix="CR_")
            cr_title = self.G.nodes[cr_id].get("Title", "")
            for desc_id in nx.descendants(self.G, cr_id):
                row = {"CR_ID": cr_id, "CR_Title": cr_title}
                row.update(cr_meta)
                row.update(
                    {
                        "Impacted_WI_ID": desc_id,
                        "Impacted_Title": self.G.nodes[desc_id].get("Title", ""),
                    }
                )
                row.update(self._meta(desc_id, prefix="Impacted_"))
                rows.append(row)
        log.info("Generated unpacked impact view (%d rows).", len(rows))
        if not rows:
            return pd.DataFrame(columns=["CR_ID", "CR_Title", "Impacted_WI_ID", "Impacted_Title"])
        return pd.DataFrame(rows)

    def analyze_cr_prereqs_unpacked(self) -> pd.DataFrame:
        """Flat 1:1 view of CRs and every individual upstream prerequisite WI."""
        cr_nodes = [
            n for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        ]
        rows = []
        for cr_id in cr_nodes:
            cr_meta = self._meta(cr_id, prefix="CR_")
            cr_title = self.G.nodes[cr_id].get("Title", "")
            for asc_id in nx.ancestors(self.G, cr_id):
                row = {"CR_ID": cr_id, "CR_Title": cr_title}
                row.update(cr_meta)
                row.update(
                    {"Prereq_WI_ID": asc_id, "Prereq_Title": self.G.nodes[asc_id].get("Title", "")}
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


def run_analyze(args: argparse.Namespace) -> None:
    """Entry point for `analyze` mode."""
    df_nodes = pd.read_parquet(args.nodes_file)
    df_edges = pd.read_parquet(args.edges_file)
    log.info("Loaded %d nodes, %d edges.", len(df_nodes), len(df_edges))

    steps_requested = (
        list(ALL_STEPS)
        if args.steps.strip().lower() == "all"
        else [s.strip().lower() for s in args.steps.split(",")]
    )

    try:
        analyzer = GraphAnalyzer(
            df_nodes,
            df_edges,
            args.ado_url,
            args.effort_field,
            bridges=args.bridges,
            max_indirect_edges=args.max_indirect_edges,
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
    for step in steps_requested:
        fn = step_fn_map.get(step)
        if fn is None:
            log.warning("Unknown analysis step '%s' – skipping.", step)
            continue
        log.info("Running step: %s …", step)
        results[step] = fn()

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    write_analysis_report(results, output_path)
