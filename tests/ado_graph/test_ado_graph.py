"""Offline behavior tests for the ADO graph/export tool using SDK-like fakes."""

from types import SimpleNamespace

import pandas as pd
import pytest
from openpyxl import load_workbook

from workflow_automations import ado_graph as tool


def nodes():
    return pd.DataFrame(
        [
            {"WI_ID": 1, "Title": "CR", "WorkItemType": "Change Request", "Effort": 2},
            {"WI_ID": 2, "Title": "Child", "WorkItemType": "Feature", "Effort": 3},
            {"WI_ID": 3, "Title": "Prerequisite", "WorkItemType": "Feature", "Effort": 5},
            {"WI_ID": 4, "Title": "Isolated", "WorkItemType": "Feature", "Effort": 7},
        ]
    )


def edges():
    return pd.DataFrame(
        [(1, 2, "Parent->Child"), (1, 3, "Successor->Predecessor"), (1, 99, "Blocks")],
        columns=["Source_ID", "Target_ID", "Link_Type"],
    )


def test_reverse_edges_and_absent_endpoints():
    analyzer = tool.GraphAnalyzer(nodes(), edges(), "https://ado.example", "Effort")
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
    analyzer = tool.GraphAnalyzer(nodes(), links)
    assert analyzer.G.number_of_edges() == 1
    assert analyzer.G[1][2]["link_type"] == "Blocks"


def test_dependency_depth_is_not_longest_chain_depth():
    links = pd.DataFrame(
        [(1, 2, "Blocks"), (2, 3, "Blocks"), (1, 3, "Blocks")], columns=edges().columns
    )
    analyzer = tool.GraphAnalyzer(nodes(), links)
    metrics = analyzer.compute_metrics().set_index("WI_ID")
    assert metrics.loc[3, "Dependency_Depth"] == 1
    assert analyzer.find_longest_chain().attrs["longest_path_length"] == 2


def test_analyze_parquet_to_all_report_sheets(tmp_path):
    node_file, edge_file = tmp_path / "nodes.parquet", tmp_path / "edges.parquet"
    nodes().to_parquet(node_file, index=False)
    edges().to_parquet(edge_file, index=False)
    output = tmp_path / "report.xlsx"
    assert (
        tool.main(
            [
                "analyze",
                "--nodes-file",
                str(node_file),
                "--edges-file",
                str(edge_file),
                "--output-file",
                str(output),
                "--effort-field",
                "Effort",
            ]
        )
        is None
    )
    workbook = load_workbook(output)
    try:
        assert workbook.sheetnames == list(tool.ALL_STEPS)
        assert workbook["orphans"]["A2"].value == 4
        assert workbook["cr_scope_impact"]["C2"].value == 5
    finally:
        workbook.close()


def test_saved_query_flat_and_link_results():
    client = object.__new__(tool.ADOClient)
    flat = SimpleNamespace(work_items=[SimpleNamespace(id=5)], work_item_relations=None)
    client._wit_client = SimpleNamespace(query_by_id=lambda **kwargs: flat)
    assert client.run_query_by_id("Example", "query") == [5]
    links = SimpleNamespace(
        work_items=None,
        work_item_relations=[
            SimpleNamespace(source=None, target=SimpleNamespace(id=1)),
            SimpleNamespace(source=SimpleNamespace(id=1), target=SimpleNamespace(id=2)),
        ],
    )
    client._wit_client = SimpleNamespace(query_by_id=lambda **kwargs: links)
    assert client.run_query_by_id("Example", "query") == [1, 2]


def wi(wi_id, wi_type="Feature", targets=(), rel="System.LinkTypes.Hierarchy-Forward"):
    return SimpleNamespace(
        id=wi_id,
        fields={"System.WorkItemType": wi_type, "System.TeamProject": "Example"},
        relations=[SimpleNamespace(rel=rel, url=f"https://ado.example/{t}") for t in targets],
    )


def fake_client(items, seeds, rejected=(), log=None):
    """SDK-like client; `rejected` IDs fail the WIQL filter, absent IDs are omitted."""
    log = log if log is not None else {"fetched": [], "filtered": []}

    class Client:
        def __init__(self, _url):
            pass

        def run_query_by_id(self, _project, _query):
            return list(seeds)

        def filter_ids_via_wiql(self, ids, _projects, _types):
            log["filtered"].extend(ids)
            return set(ids) - set(rejected)

        def get_work_items_batch(self, ids):
            log["fetched"].extend(ids)
            return [items[i] for i in ids if i in items]

    return Client, log


def run_export(monkeypatch, tmp_path, client, *extra):
    monkeypatch.setattr(tool, "ADOClient", client)
    monkeypatch.setattr(tool, "configure_ca_bundle", lambda _path: None)
    tool.main(
        [
            "export",
            "--ado-url",
            "https://ado.example",
            "--query-id",
            "synthetic",
            "--output-dir",
            str(tmp_path),
            "--formats",
            "parquet",
            *extra,
        ]
    )
    return pd.read_parquet(tmp_path / "nodes.parquet"), pd.read_parquet(tmp_path / "edges.parquet")


def test_follow_traverses_filtered_bridge_at_pruning_scale(tmp_path, monkeypatch):
    """AG-001: a queue of three batches no longer prunes the skipped bridge 3."""
    items = {1: wi(1), 2: wi(2), 3: wi(3, "Task", [4]), 4: wi(4)}
    client, log = fake_client(items, [1, 2, 3])
    nodes, _ = run_export(monkeypatch, tmp_path, client, "--batch-size", "1")
    assert log["filtered"] == []
    assert log["fetched"] == [1, 2, 3, 4]
    assert dict(zip(nodes["WI_ID"], nodes["In_Scope"], strict=True)) == {
        1: True,
        2: True,
        3: False,
        4: True,
    }


def test_filtered_bridge_keeps_features_connected_in_analysis(tmp_path, monkeypatch):
    """AG-015: Feature 1 -> Task 2 -> Feature 3 stays one connected path."""
    items = {1: wi(1, targets=[2]), 2: wi(2, "Task", [3]), 3: wi(3)}
    client, _ = fake_client(items, [1])
    nodes, edges = run_export(monkeypatch, tmp_path, client)
    assert set(edges[["Source_ID", "Target_ID"]].itertuples(index=False, name=None)) == {
        (1, 2),
        (2, 3),
    }
    analyzer = tool.GraphAnalyzer(nodes, edges)
    assert set(analyzer.G.nodes) == {1, 3}
    assert analyzer.G[1][3]["link_type"] == "Indirect"
    assert analyzer.G[1][3]["via"] == (2,)
    assert analyzer.find_orphans().empty
    assert "via 2 (Parent->Child, Parent->Child)" in analyzer._get_neighborhood_diagnostic(3)


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
    analyzer = tool.GraphAnalyzer(graph_nodes, links, effort_field="Effort")
    assert set(analyzer.G.edges) == {(1, 4), (1, 5)}
    assert analyzer.G[1][4]["via"] == (2, 3)
    assert analyzer.G[1][4]["path"] == ("Parent->Child", "Blocks", "Affects")
    assert analyzer.G[1][5]["link_type"] == "Blocks"
    impact = analyzer.analyze_cr_scope_and_impact().iloc[0]
    assert impact["Impacted_WI_Count"] == 2
    assert impact["Total_Effort"] == 6  # bridge efforts are not counted


def test_stop_mode_classifies_each_queued_id_once(tmp_path, monkeypatch):
    """AG-014: 2,000 seeds at batch 100 send each ID to WIQL once (was 18,900)."""
    items = {i: wi(i) for i in range(1, 2001)}
    client, log = fake_client(items, range(1, 2001), rejected={7})
    nodes, _ = run_export(
        monkeypatch, tmp_path, client, "--batch-size", "100", "--filtered-relations", "stop"
    )
    assert len(log["filtered"]) == len(set(log["filtered"])) == 2000
    assert 7 not in log["fetched"]
    assert len(log["fetched"]) == len(set(log["fetched"])) == 1999
    assert len(nodes) == 1999


def test_stop_mode_does_not_follow_filtered_items(tmp_path, monkeypatch):
    items = {1: wi(1, targets=[2, 3]), 2: wi(2, "Task", [4]), 3: wi(3, "Bug", [5]), 4: wi(4)}
    items[5] = wi(5)
    # 3 passes WIQL (as after a failed WIQL chunk), so it is fetched but not followed.
    client, log = fake_client(items, [1], rejected={2})
    nodes, _ = run_export(monkeypatch, tmp_path, client, "--filtered-relations", "stop")
    assert log["fetched"] == [1, 3]
    assert dict(zip(nodes["WI_ID"], nodes["In_Scope"], strict=True)) == {1: True, 3: False}


def test_omitted_ids_are_reported(tmp_path, monkeypatch, capsys):
    """AG-003 (partial): IDs ADO does not return are counted, not silently lost."""
    client, _ = fake_client({1: wi(1, targets=[2])}, [1])
    nodes, _ = run_export(monkeypatch, tmp_path, client)
    assert list(nodes["WI_ID"]) == [1]
    assert "Omitted by ADO (not returned): 1" in capsys.readouterr().out


def test_batch_size_must_be_positive(tmp_path, monkeypatch):
    client, log = fake_client({1: wi(1)}, [1])
    with pytest.raises(SystemExit):
        run_export(monkeypatch, tmp_path, client, "--batch-size", "0")
    assert log["fetched"] == []
