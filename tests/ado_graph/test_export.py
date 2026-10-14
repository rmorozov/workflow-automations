"""Offline behavior tests for ado-graph export using SDK-like fakes."""

from ado_graph_data import fake_client, run_export, wi

from workflow_automations.ado_graph import analyze as tool


def test_follow_traverses_filtered_bridge_at_pruning_scale(tmp_path, monkeypatch):
    """AG-001: a queue of three batches no longer prunes the skipped bridge 3."""
    items = {1: wi(1), 2: wi(2), 3: wi(3, "Task", [4]), 4: wi(4)}
    client, log = fake_client(items, [1, 2, 3])
    nodes, _ = run_export(
        monkeypatch, tmp_path, client, "--batch-size", "1", "--filtered-relations", "follow"
    )
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
    nodes, edges = run_export(monkeypatch, tmp_path, client, "--filtered-relations", "follow")
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


def test_default_stop_mode_classifies_each_queued_id_once(tmp_path, monkeypatch):
    """Default mode; AG-014: 2,000 seeds at batch 100 send each ID to WIQL once (was 18,900)."""
    items = {i: wi(i) for i in range(1, 2001)}
    client, log = fake_client(items, range(1, 2001), rejected={7})
    nodes, _ = run_export(monkeypatch, tmp_path, client, "--batch-size", "100")
    assert len(log["filtered"]) == len(set(log["filtered"])) == 2000
    assert 7 not in log["fetched"]
    assert len(log["fetched"]) == len(set(log["fetched"])) == 1999
    assert len(nodes) == 1999


def test_stop_mode_does_not_follow_filtered_items(tmp_path, monkeypatch):
    items = {1: wi(1, targets=[2, 3]), 2: wi(2, "Task", [4]), 3: wi(3, "Bug", [5]), 4: wi(4)}
    items[5] = wi(5)
    # 3 passes WIQL (as after a failed WIQL chunk), so it is fetched but neither
    # followed nor exported.
    client, log = fake_client(items, [1], rejected={2})
    nodes, _ = run_export(monkeypatch, tmp_path, client, "--filtered-relations", "stop")
    assert log["fetched"] == [1, 3]
    assert dict(zip(nodes["WI_ID"], nodes["In_Scope"], strict=True)) == {1: True}


def test_stop_mode_output_is_the_same_with_and_without_wiql_fallback(tmp_path, monkeypatch):
    """Review finding: stop-mode results must not depend on the WIQL filter succeeding."""
    items = {1: wi(1, targets=[2]), 2: wi(2, "Task", [3]), 3: wi(3)}
    results = []
    for name, rejected in (("pruned", {2}), ("fallback", set())):
        client, log = fake_client(items, [1, 3], rejected=rejected)
        out = tmp_path / name
        nodes, edges = run_export(monkeypatch, out, client)
        analyzer = tool.GraphAnalyzer(nodes, edges)
        results.append(
            (
                sorted(nodes["WI_ID"]),
                sorted(edges[["Source_ID", "Target_ID"]].itertuples(index=False, name=None)),
                sorted(analyzer.G.edges),
                sorted(analyzer.find_orphans()["WI_ID"]),
            )
        )
        assert (2 in log["fetched"]) == (name == "fallback")
    assert results[0] == results[1] == ([1, 3], [(1, 2)], [], [1, 3])


def test_omitted_ids_are_reported(tmp_path, monkeypatch, capsys):
    """AG-003 (partial): IDs ADO does not return are counted, not silently lost."""
    client, _ = fake_client({1: wi(1, targets=[2])}, [1])
    nodes, _ = run_export(monkeypatch, tmp_path, client)
    assert list(nodes["WI_ID"]) == [1]
    assert "Omitted by ADO (not returned): 1" in capsys.readouterr().out
