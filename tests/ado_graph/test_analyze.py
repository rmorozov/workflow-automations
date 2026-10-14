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
    # has_cr_dep is the CR plus what depends on it (downstream), not its prerequisites.
    assert set(analyzer.find_has_cr_dep()["WI_ID"]) == {1, 2}
    assert set(analyzer.find_no_cr_dep()["WI_ID"]) == {3, 4}


def test_parallel_relation_types_are_all_kept():
    links = pd.DataFrame(
        [(1, 2, "Blocks"), (1, 2, "Parent->Child"), (2, 1, "Blocked By"), (1, 2, "Custom")],
        columns=edges().columns,
    )
    analyzer = analyze.GraphAnalyzer(nodes(), links)
    # Blocks reported from both ends is one relation; Parent->Child stays beside it.
    assert sorted(analyzer.relations.edges(keys=True)) == [
        (1, 2, "Blocks"),
        (1, 2, "Parent->Child"),
    ]
    assert analyzer.G[1][2]["link_types"] == ("Parent->Child", "Blocks")
    assert analyzer.G[1][2]["link_type"] == "Parent->Child & Blocks"
    assert analyzer.graph("hierarchy")[1][2]["link_types"] == ("Parent->Child",)
    assert analyzer.graph("dependency")[1][2]["link_types"] == ("Blocks",)
    assert analyzer.graph("impact").number_of_edges() == 0
    orphans = analyzer.find_orphans()
    assert set(orphans["WI_ID"]) == {3, 4}
    debug = analyzer.find_has_cr_dep().set_index("WI_ID").loc[1, "Graph_Neighbors_Debug"]
    assert debug == "-> [Parent->Child & Blocks] to Feature (2)"


def test_unknown_link_types_are_reported(caplog):
    links = pd.DataFrame([(1, 2, "Related"), (1, 3, "Related")], columns=edges().columns)
    with caplog.at_level("WARNING", logger="ado_tool"):
        analyzer = analyze.GraphAnalyzer(nodes(), links)
    assert analyzer.G.number_of_edges() == 0
    assert "Ignored 2 edge(s) with unknown link type 'Related'" in caplog.text


def cr_neighbourhood():
    """CR 1 with a child, a parent, a predecessor, a blocked item and an affected item."""
    graph_nodes = pd.DataFrame(
        [
            {"WI_ID": 1, "WorkItemType": "Change Request", "Effort": 1},
            {"WI_ID": 2, "WorkItemType": "Task", "Effort": 2},  # child
            {"WI_ID": 3, "WorkItemType": "Epic", "Effort": 4},  # parent
            {"WI_ID": 4, "WorkItemType": "Feature", "Effort": 8},  # predecessor
            {"WI_ID": 5, "WorkItemType": "Feature", "Effort": 16},  # blocked by the CR
            {"WI_ID": 6, "WorkItemType": "Requirement", "Effort": 32},  # affected
            {"WI_ID": 7, "WorkItemType": "Feature", "Effort": 64},  # unrelated
        ]
    )
    links = pd.DataFrame(
        [
            (1, 2, "Parent->Child"),
            (1, 3, "Child->Parent"),
            (4, 1, "Predecessor->Successor"),
            (5, 1, "Blocked By"),
            (1, 6, "Affects"),
            (4, 7, "Blocks"),
        ],
        columns=edges().columns,
    )
    return analyze.GraphAnalyzer(graph_nodes, links, effort_field="Effort")


def ids(cell):
    return {int(i) for i in str(cell).split(",") if i}


def test_cr_steps_use_their_documented_subgraphs():
    analyzer = cr_neighbourhood()
    impact = analyzer.analyze_cr_scope_and_impact().iloc[0]
    assert ids(impact["Impacted_WI_IDs"]) == {2, 5, 6}
    assert impact["Total_Effort"] == 1 + 2 + 16 + 32
    hierarchy = analyzer.analyze_cr_scope_and_impact("hierarchy").iloc[0]
    assert ids(hierarchy["Impacted_WI_IDs"]) == {2}
    dependency = analyzer.analyze_cr_scope_and_impact("dependency").iloc[0]
    assert ids(dependency["Impacted_WI_IDs"]) == {5}
    assert set(analyzer.analyze_cr_impact_unpacked("impact")["Impacted_WI_ID"]) == {6}

    # Prerequisites follow dependency relations only: the parent is not one.
    assert ids(analyzer.analyze_cr_prerequisites().iloc[0]["Prerequisite_WI_IDs"]) == {4}
    assert set(analyzer.analyze_cr_prereqs_unpacked()["Prereq_WI_ID"]) == {4}
    assert ids(analyzer.analyze_cr_prerequisites("all").iloc[0]["Prerequisite_WI_IDs"]) == {3, 4}

    assert set(analyzer.find_has_cr_dep()["WI_ID"]) == {1, 2, 5, 6}
    assert set(analyzer.find_no_cr_dep()["WI_ID"]) == {3, 4, 7}
    assert set(analyzer.find_has_cr_dep("dependency")["WI_ID"]) == {1, 5}


def test_structural_steps_accept_a_subgraph():
    graph_nodes = nodes()
    links = pd.DataFrame(
        [(1, 2, "Parent->Child"), (2, 1, "Blocks"), (2, 3, "Blocks")], columns=edges().columns
    )
    analyzer = analyze.GraphAnalyzer(graph_nodes, links)
    assert len(analyzer.find_cycles()) == 2  # child blocks its parent: a mixed cycle
    assert analyzer.find_cycles("dependency").empty
    assert analyzer.find_longest_chain("dependency").attrs["longest_path_length"] == 1
    assert set(analyzer.find_orphans("hierarchy")["WI_ID"]) == {3, 4}
    assert set(analyzer.find_scc("all")["SCC_Size"]) == {1, 2}
    metrics = analyzer.compute_metrics("hierarchy").set_index("WI_ID")
    assert metrics.loc[1, "Out_Degree"] == 1 and metrics.loc[3, "In_Degree"] == 0
    with pytest.raises(ValueError, match="unknown subgraph"):
        analyzer.graph("hierarchy+dependency")


def test_bridges_contract_within_each_subgraph():
    graph_nodes = pd.DataFrame(
        [
            {"WI_ID": 1, "WorkItemType": "Feature", "In_Scope": True},
            {"WI_ID": 2, "WorkItemType": "Task", "In_Scope": False},
            {"WI_ID": 3, "WorkItemType": "Feature", "In_Scope": True},
            {"WI_ID": 4, "WorkItemType": "Feature", "In_Scope": True},
        ]
    )
    links = pd.DataFrame(
        [(1, 2, "Parent->Child"), (1, 2, "Blocks"), (2, 3, "Blocks"), (2, 4, "Parent->Child")],
        columns=edges().columns,
    )
    analyzer = analyze.GraphAnalyzer(graph_nodes, links)
    assert analyzer.G[1][3]["path"] == ("Parent->Child & Blocks", "Blocks")
    assert analyzer.G[1][4]["path"] == ("Parent->Child & Blocks", "Parent->Child")
    dependency = analyzer.graph("dependency")
    assert set(dependency.edges) == {(1, 3)}
    assert dependency[1][3]["path"] == ("Blocks", "Blocks")
    assert set(analyzer.graph("hierarchy").edges) == {(1, 4)}
    assert 2 not in analyzer.graph("impact")


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


def test_steps_name_their_subgraph_and_sheet(tmp_path, caplog):
    analyzer = cr_neighbourhood()
    node_file, edge_file = tmp_path / "nodes.parquet", tmp_path / "edges.parquet"
    analyzer.df_nodes.to_parquet(node_file, index=False)
    analyzer.df_edges.to_parquet(edge_file, index=False)
    output = tmp_path / "report.xlsx"
    with caplog.at_level("WARNING", logger="ado_tool"):
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
                "cr_prereqs, CR_Prereqs@All,cr_prereqs_unpacked@dependency,scc@typo,nope",
            ]
        )
    sheets = pd.read_excel(output, sheet_name=None)
    assert list(sheets) == ["cr_prereqs", "cr_prereqs@all", "cr_prereqs_unpacked@dependency"]
    assert ids(sheets["cr_prereqs"].iloc[0]["Prerequisite_WI_IDs"]) == {4}
    assert ids(sheets["cr_prereqs@all"].iloc[0]["Prerequisite_WI_IDs"]) == {3, 4}
    assert "Unknown subgraph in 'scc@typo'" in caplog.text
    assert "Unknown analysis step 'nope'" in caplog.text


def test_every_step_has_a_default_subgraph_and_short_sheet_names():
    assert set(analyze.STEP_SUBGRAPH) == set(analyze.ALL_STEPS)
    assert set(analyze.STEP_SUBGRAPH.values()) <= set(analyze.SUBGRAPHS)
    longest = max(f"{step}@{sub}" for step in analyze.ALL_STEPS for sub in analyze.SUBGRAPHS)
    assert all(
        len(f"{step}@{sub}") <= 31 for step in analyze.ALL_STEPS for sub in analyze.SUBGRAPHS
    ), longest
