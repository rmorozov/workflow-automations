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


def test_dependency_depth_is_longest_depth_not_shortest_distance():
    # The shortcut 1->3 gives 3 a shortest distance of 1; its longest depth is 2.
    links = pd.DataFrame(
        [(1, 2, "Blocks"), (2, 3, "Blocks"), (1, 3, "Blocks")], columns=edges().columns
    )
    analyzer = analyze.GraphAnalyzer(nodes(), links)
    metrics = analyzer.compute_metrics().set_index("WI_ID")
    assert metrics["Dependency_Depth"].to_dict() == {1: 0, 2: 1, 3: 2, 4: 0}
    chain = analyzer.find_longest_chain()
    assert chain.attrs["longest_path_length"] == 2
    assert list(chain["WI_ID"]) == [1, 2, 3]
    assert set(chain["Chain_Length"]) == {2}
    assert metrics["Dependency_Depth"].max() == chain.attrs["longest_path_length"]


def test_dependency_depth_counts_a_cycle_as_one_step():
    # 2<->3 is one SCC: both members share its depth, and 4 is one hop below it.
    links = pd.DataFrame(
        [(1, 2, "Blocks"), (2, 3, "Blocks"), (3, 2, "Blocks"), (3, 4, "Blocks"), (1, 4, "Blocks")],
        columns=edges().columns,
    )
    analyzer = analyze.GraphAnalyzer(nodes(), links)
    metrics = analyzer.compute_metrics().set_index("WI_ID")
    assert metrics["Dependency_Depth"].to_dict() == {1: 0, 2: 1, 3: 1, 4: 2}
    assert set(analyzer.find_longest_chain()["Chain_Length"]) == {2}


def test_longest_chain_length_is_written_to_the_report(tmp_path):
    links = pd.DataFrame(
        [(1, 2, "Blocks"), (2, 3, "Blocks"), (1, 3, "Blocks")], columns=edges().columns
    )
    analyzer = analyze.GraphAnalyzer(nodes(), links)
    output = tmp_path / "report.xlsx"
    analyze.write_analysis_report(
        {"longest_chain": analyzer.find_longest_chain(), "metrics": analyzer.compute_metrics()},
        output,
    )
    sheets = pd.read_excel(output, sheet_name=None)
    assert set(sheets["longest_chain"]["Chain_Length"]) == {2}
    assert sheets["metrics"].set_index("WI_ID").loc[3, "Dependency_Depth"] == 2


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
            "cr_prereqs, CR_Prereqs@All,cr_prereqs_unpacked@dependency",
        ]
    )
    sheets = pd.read_excel(output, sheet_name=None)
    assert list(sheets) == ["cr_prereqs", "cr_prereqs@all", "cr_prereqs_unpacked@dependency"]
    assert ids(sheets["cr_prereqs"].iloc[0]["Prerequisite_WI_IDs"]) == {4}
    assert ids(sheets["cr_prereqs@all"].iloc[0]["Prerequisite_WI_IDs"]) == {3, 4}
    # The command line rejects these first (test_cli); the parser alone skips them.
    with caplog.at_level("WARNING", logger="ado_tool"):
        assert analyze.parse_steps("scc@typo,nope") == []
    assert "Unknown subgraph in 'scc@typo'" in caplog.text
    assert "Unknown analysis step 'nope'" in caplog.text


def test_every_step_has_a_default_subgraph_and_short_sheet_names():
    assert set(analyze.STEP_SUBGRAPH) == set(analyze.ALL_STEPS)
    assert set(analyze.STEP_SUBGRAPH.values()) <= set(analyze.SUBGRAPHS)
    longest = max(
        (f"{step}@{sub}" for step in analyze.ALL_STEPS for sub in analyze.SUBGRAPHS), key=len
    )
    assert all(
        len(f"{step}@{sub}") <= 31 for step in analyze.ALL_STEPS for sub in analyze.SUBGRAPHS
    ), longest


def test_all_combines_with_subgraph_tokens():
    parsed = analyze.parse_steps("all, cycles @ Dependency,")
    assert [sheet for sheet, _, _ in parsed] == [*analyze.ALL_STEPS, "cycles@dependency"]
    assert parsed[-1][1:] == ("cycles", "dependency")


def test_items_implementing_or_affecting_a_cr_are_upstream():
    graph_nodes = pd.DataFrame(
        [
            {"WI_ID": 1, "WorkItemType": "Change Request"},
            {"WI_ID": 2, "WorkItemType": "Task"},
            {"WI_ID": 3, "WorkItemType": "Requirement"},
        ]
    )
    links = pd.DataFrame([(2, 1, "Implements"), (3, 1, "Affects")], columns=edges().columns)
    analyzer = analyze.GraphAnalyzer(graph_nodes, links)
    assert set(analyzer.find_no_cr_dep()["WI_ID"]) == {2, 3}
    assert analyzer.analyze_cr_prerequisites().iloc[0]["Prerequisite_WI_Count"] == 0
    assert ids(analyzer.analyze_cr_prerequisites("impact").iloc[0]["Prerequisite_WI_IDs"]) == {2, 3}


def complete_digraph(n):
    """Every ordered pair blocks: the number of simple cycles grows factorially."""
    graph_nodes = pd.DataFrame({"WI_ID": range(n), "WorkItemType": "Task"})
    links = pd.DataFrame(
        [(i, j, "Blocks") for i in range(n) for j in range(n) if i != j],
        columns=edges().columns,
    )
    return graph_nodes, links


def test_cycle_enumeration_stops_at_its_budget(caplog):
    # K12 has over 10^8 simple cycles; unbounded enumeration would not finish.
    analyzer = analyze.GraphAnalyzer(*complete_digraph(12), max_cycles=50)
    with caplog.at_level("WARNING", logger="ado_tool"):
        cycles = analyzer.find_cycles()
    assert cycles["Cycle_Index"].nunique() == 50
    assert set(cycles["Cycles_Complete"]) == {False}
    assert cycles.attrs["cycles_complete"] is False
    assert "stopped at its budget" in caplog.text
    # Every node is still reported as part of one strongly connected component.
    assert set(analyzer.find_scc()["SCC_Size"]) == {12}

    by_rows = analyze.GraphAnalyzer(*complete_digraph(12), max_cycle_rows=30).find_cycles()
    # Stops after the cycle that reaches the row budget, so it overshoots by < one cycle.
    assert 30 <= len(by_rows) < 30 + 12
    assert set(by_rows["Cycles_Complete"]) == {False}


def test_cycles_exactly_at_the_budget_are_complete():
    # A triangle has exactly two simple cycles here: 1->2->3->1 and 1<->2.
    links = pd.DataFrame(
        [(1, 2, "Blocks"), (2, 3, "Blocks"), (3, 1, "Blocks"), (2, 1, "Blocks")],
        columns=edges().columns,
    )
    cycles = analyze.GraphAnalyzer(nodes(), links, max_cycles=2).find_cycles()
    assert cycles["Cycle_Index"].nunique() == 2
    assert set(cycles["Cycles_Complete"]) == {True}
    assert (
        analyze.GraphAnalyzer(nodes(), links, max_cycles=1).find_cycles().attrs["cycles_complete"]
        is False
    )
    empty = analyze.GraphAnalyzer(nodes(), edges()).find_cycles()
    assert empty.empty and "Cycles_Complete" in empty.columns


def test_betweenness_is_exact_within_budget_and_sampled_above_it():
    # 1->2->3->4: the inner nodes carry every path through them.
    links = pd.DataFrame(
        [(1, 2, "Blocks"), (2, 3, "Blocks"), (3, 4, "Blocks")], columns=edges().columns
    )
    exact = analyze.GraphAnalyzer(nodes(), links).compute_metrics().set_index("WI_ID")
    assert set(exact["Betweenness_Sources"]) == {4}
    assert exact.loc[2, "Betweenness_Centrality"] == pytest.approx(2 / 6)

    chain = pd.DataFrame({"WI_ID": range(400), "WorkItemType": "Task"})
    chain_links = pd.DataFrame([(i, i + 1, "Blocks") for i in range(399)], columns=edges().columns)
    sampled = analyze.GraphAnalyzer(chain, chain_links, centrality_sources=100)
    first = sampled.compute_metrics()
    assert first.attrs["betweenness_sources"] == 100
    assert set(first["Betweenness_Sources"]) == {100}
    # A fixed seed: repeat runs give the same estimate.
    assert first["Betweenness_Centrality"].equals(
        sampled.compute_metrics()["Betweenness_Centrality"]
    )
    exact_chain = analyze.GraphAnalyzer(chain, chain_links, centrality_sources=400)
    reference = exact_chain.compute_metrics().set_index("WI_ID")["Betweenness_Centrality"]
    estimate = first.set_index("WI_ID")["Betweenness_Centrality"]
    # The middle of a chain is its most central node in the exact and the sampled result.
    assert abs(estimate.idxmax() - reference.idxmax()) < 40
    assert estimate.loc[200] == pytest.approx(reference.loc[200], rel=0.25)


def write_inputs(tmp_path, graph_nodes, links):
    node_file, edge_file = tmp_path / "nodes.parquet", tmp_path / "edges.parquet"
    graph_nodes.to_parquet(node_file, index=False)
    links.to_parquet(edge_file, index=False)
    return ["--nodes-file", str(node_file), "--edges-file", str(edge_file)]


def test_dense_cycles_give_a_partial_report(tmp_path):
    output = tmp_path / "report.xlsx"
    inputs = write_inputs(tmp_path, *complete_digraph(10))
    tool.main(["analyze", *inputs, "--output-file", str(output), "--steps", "cycles,scc"])
    sheets = pd.read_excel(output, sheet_name=None)
    assert sheets["cycles"]["Cycle_Index"].nunique() == analyze.DEFAULT_MAX_CYCLES
    assert set(sheets["cycles"]["Cycles_Complete"]) == {False}
    assert len(sheets["scc"]) == 10


def test_interrupted_step_writes_no_report(tmp_path, monkeypatch, caplog):
    output = tmp_path / "report.xlsx"
    inputs = write_inputs(tmp_path, nodes(), edges())

    def interrupt(self, subgraph=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(analyze.GraphAnalyzer, "compute_metrics", interrupt)
    with caplog.at_level("INFO", logger="ado_tool"), pytest.raises(SystemExit) as exit_info:
        tool.main(["analyze", *inputs, "--output-file", str(output), "--steps", "scc,metrics"])
    assert exit_info.value.code == 130
    assert not output.exists()
    assert "Running step 2/2: metrics" in caplog.text
    assert "Step scc finished" in caplog.text
    assert "Interrupted during step metrics" in caplog.text


def test_cr_scope_without_crs_names_each_column_once():
    links = pd.DataFrame([(2, 3, "Blocks")], columns=edges().columns)
    no_crs = nodes()[nodes()["WorkItemType"] != "Change Request"]
    for effort_field in ("", "Effort"):
        empty = analyze.GraphAnalyzer(no_crs, links, effort_field=effort_field)
        columns = list(empty.analyze_cr_scope_and_impact().columns)
        assert len(columns) == len(set(columns))
        assert "Impacted_WI_Count" in columns
    assert list(empty.analyze_cr_scope_and_impact().columns)[2] == "Total_Effort"
