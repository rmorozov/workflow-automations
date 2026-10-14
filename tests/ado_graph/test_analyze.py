"""Offline behavior tests for ado-graph analysis and bridge contraction."""

import pandas as pd
import pytest
from ado_graph_data import edges, fan_bridge, nodes

from workflow_automations import ado_graph as tool
from workflow_automations.ado_graph import analyze


def test_reverse_edges_and_absent_endpoints():
    analyzer = analyze.GraphAnalyzer(nodes(), edges(), "https://ado.example", "Effort")
    assert set(analyzer.G.edges) == {(1, 2), (3, 1)}
    assert analyzer.G[3][1]["link_type"] == "Predecessor->Successor"
    assert set(analyzer.find_orphans()["WI_ID"]) == {4}
    assert set(analyzer.analyze_cr_scope_and_impact().iloc[0]["Impacted_WI_IDs"].split(",")) == {
        "2"
    }
    assert analyzer.analyze_cr_scope_and_impact().iloc[0]["Total_Effort"] == 5
    assert analyzer.analyze_cr_prerequisites().iloc[0]["Prerequisite_WI_IDs"] == "3"
    # Existing has_cr_dep means CR + ancestors, unlike downstream scope/impact.
    assert set(analyzer.find_has_cr_dep()["WI_ID"]) == {1, 3}


def test_parallel_relation_labels_currently_collapse():
    links = pd.DataFrame([(1, 2, "Parent->Child"), (1, 2, "Blocks")], columns=edges().columns)
    analyzer = analyze.GraphAnalyzer(nodes(), links)
    assert analyzer.G.number_of_edges() == 1
    assert analyzer.G[1][2]["link_type"] == "Blocks"


def test_dependency_depth_is_not_longest_chain_depth():
    links = pd.DataFrame(
        [(1, 2, "Blocks"), (2, 3, "Blocks"), (1, 3, "Blocks")], columns=edges().columns
    )
    analyzer = analyze.GraphAnalyzer(nodes(), links)
    metrics = analyzer.compute_metrics().set_index("WI_ID")
    assert metrics.loc[3, "Dependency_Depth"] == 1
    assert analyzer.find_longest_chain().attrs["longest_path_length"] == 2


def test_bridge_chains_contract_and_direct_links_win():
    graph_nodes = pd.DataFrame(
        [
            {"WI_ID": 1, "WorkItemType": "Change Request", "In_Scope": True, "Effort": 1},
            {"WI_ID": 2, "WorkItemType": "Task", "In_Scope": False, "Effort": 100},
            {"WI_ID": 3, "WorkItemType": "Bug", "In_Scope": False, "Effort": 100},
            {"WI_ID": 4, "WorkItemType": "Feature", "In_Scope": True, "Effort": 2},
            {"WI_ID": 5, "WorkItemType": "Feature", "In_Scope": True, "Effort": 3},
        ]
    )
    links = pd.DataFrame(
        [
            (1, 2, "Parent->Child"),
            (2, 3, "Blocks"),
            (4, 3, "Affected By"),
            (1, 5, "Blocks"),
            (2, 5, "Parent->Child"),
        ],
        columns=["Source_ID", "Target_ID", "Link_Type"],
    )
    analyzer = analyze.GraphAnalyzer(graph_nodes, links, effort_field="Effort")
    assert set(analyzer.G.edges) == {(1, 4), (1, 5)}
    assert analyzer.G[1][4]["via"] == (2, 3)
    assert analyzer.G[1][4]["path"] == ("Parent->Child", "Blocks", "Affects")
    assert analyzer.G[1][5]["link_type"] == "Blocks"
    impact = analyzer.analyze_cr_scope_and_impact().iloc[0]
    assert impact["Impacted_WI_Count"] == 2
    assert impact["Total_Effort"] == 6  # bridge efforts are not counted


def test_bridge_contraction_budget():
    graph_nodes, links = fan_bridge(50)
    assert analyze.GraphAnalyzer(graph_nodes, links).G.number_of_edges() == 2500
    with pytest.raises(analyze.ContractionBudgetExceeded, match="2,500"):
        analyze.GraphAnalyzer(graph_nodes, links, max_indirect_edges=2499)
    dropped = analyze.GraphAnalyzer(graph_nodes, links, bridges="drop", max_indirect_edges=1)
    assert dropped.G.number_of_edges() == 0
    assert 0 not in dropped.G


def test_exceeded_budget_fails_before_writing_report(tmp_path):
    graph_nodes, links = fan_bridge(50)
    node_file, edge_file = tmp_path / "nodes.parquet", tmp_path / "edges.parquet"
    graph_nodes.to_parquet(node_file, index=False)
    links.to_parquet(edge_file, index=False)
    output = tmp_path / "out" / "report.xlsx"
    with pytest.raises(SystemExit, match="--max-indirect-edges"):
        tool.main(
            [
                "analyze",
                "--nodes-file",
                str(node_file),
                "--edges-file",
                str(edge_file),
                "--output-file",
                str(output),
                "--steps",
                "orphans",
                "--max-indirect-edges",
                "100",
            ]
        )
    assert not output.parent.exists()
