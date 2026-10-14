"""Offline behavior tests for ado-graph export using SDK-like fakes."""

import collections
import re
from types import SimpleNamespace

import pandas as pd
import pytest
from ado_graph_data import fake_client, run_export, wi

from workflow_automations.ado_graph import analyze, client, export


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
    analyzer = analyze.GraphAnalyzer(nodes, edges)
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
        analyzer = analyze.GraphAnalyzer(nodes, edges)
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


def test_omitted_linked_ids_write_outputs_with_incomplete_status(tmp_path, monkeypatch, capsys):
    """AG-003: a linked ID ADO does not return is counted and exits 4, outputs written."""
    items = {1: wi(1, targets=[2, 3]), 3: wi(3, targets=[4])}
    fake, _ = fake_client(items, [1])
    with pytest.raises(SystemExit) as exc:
        run_export(monkeypatch, tmp_path, fake)
    assert exc.value.code == export.EXIT_INCOMPLETE == 4
    assert sorted(pd.read_parquet(tmp_path / "nodes.parquet")["WI_ID"]) == [1, 3]
    out = capsys.readouterr().out
    assert "Omitted by ADO (not returned): 2" in out
    assert "seeds (from the saved query): 0" in out
    assert "linked (reached via relations): 2" in out
    assert "Completion status: incomplete" in out


def test_omitted_seed_fails_fast_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """AG-003: a missing seed is a failed scope (exit 5); linked IDs are not fetched."""
    items = {1: wi(1, targets=[3]), 3: wi(3)}
    fake, log = fake_client(items, [1, 2])
    with pytest.raises(SystemExit) as exc:
        run_export(monkeypatch, tmp_path, fake, "--batch-size", "1")
    assert exc.value.code == export.EXIT_SEEDS_MISSING == 5
    assert log["fetched"] == [1, 2]
    assert list(tmp_path.iterdir()) == []
    out = capsys.readouterr().out
    assert "seeds (from the saved query): 1" in out
    assert "Completion status: failed" in out


def test_failed_run_counts_every_missing_seed(tmp_path, monkeypatch, capsys):
    """Review finding: the seed count must not depend on the batch size."""
    fake, log = fake_client({2: wi(2, targets=[4]), 4: wi(4)}, [1, 2, 3])
    with pytest.raises(SystemExit):
        run_export(monkeypatch, tmp_path, fake, "--batch-size", "1")
    assert log["fetched"] == [1, 2, 3]
    assert "seeds (from the saved query): 2" in capsys.readouterr().out


def test_complete_export_returns_normally(tmp_path, monkeypatch, capsys):
    fake, _ = fake_client({1: wi(1, targets=[2]), 2: wi(2)}, [1])
    nodes, _ = run_export(monkeypatch, tmp_path, fake)
    assert sorted(nodes["WI_ID"]) == [1, 2]
    assert "Completion status: complete" in capsys.readouterr().out


def wiql_client(items, seeds, log, fail_existence=False):
    """Real ADOClient over a fake SDK whose WIQL reads IDs and the type filter."""

    def query_by_wiql(wiql, top):
        text = wiql.query
        ids = [int(i) for i in re.search(r"IN \(([\d,]+)\)", text).group(1).split(",")]
        filtered = "WorkItemType" in text
        log["wiql"].append((filtered, ids))
        if fail_existence and not filtered:
            raise RuntimeError("synthetic WIQL failure")
        found = [i for i in ids if i in items]
        if filtered:
            found = [i for i in found if items[i].fields["System.WorkItemType"] != "Task"]
        return SimpleNamespace(work_items=[SimpleNamespace(id=i) for i in found])

    def get_work_items_batch(request):
        log["fetched"].extend(request.ids)
        log.setdefault("batches", []).append(list(request.ids))
        assert request.error_policy == "Omit"
        return [items[i] for i in request.ids if i in items]

    def factory(_url):
        ado = object.__new__(client.ADOClient)
        ado._wit_client = SimpleNamespace(
            query_by_id=lambda **_: SimpleNamespace(
                work_items=[SimpleNamespace(id=i) for i in seeds], work_item_relations=None
            ),
            query_by_wiql=query_by_wiql,
            get_work_items_batch=get_work_items_batch,
        )
        return ado

    return factory


@pytest.mark.parametrize("fail_existence", [False, True])
def test_stop_mode_counts_deleted_link_as_omitted_not_pruned(
    tmp_path, monkeypatch, capsys, fail_existence
):
    """AG-003: WIQL also rejects deleted IDs; they must not pass as filtered out."""
    items = {1: wi(1, targets=[2, 3]), 2: wi(2, "Task")}
    log = {"wiql": [], "fetched": []}
    factory = wiql_client(items, [1], log, fail_existence)
    with pytest.raises(SystemExit) as exc:
        run_export(monkeypatch, tmp_path, factory, "--skip-types", "Task")
    assert exc.value.code == export.EXIT_INCOMPLETE
    # Task 2 exists, so it is pruned without a fetch; deleted 3 is fetched and omitted.
    # If the existence check fails, both rejected IDs are fetched and classified after.
    assert log["fetched"] == ([1, 2, 3] if fail_existence else [1, 3])
    assert (False, [2, 3]) in log["wiql"]
    assert sorted(pd.read_parquet(tmp_path / "nodes.parquet")["WI_ID"]) == [1]
    out = capsys.readouterr().out
    assert f"Pruned via WIQL (not fetched): {0 if fail_existence else 1}" in out
    assert "linked (reached via relations): 1" in out


# ---------------------------------------------------------------------------
# AG-011: counter reconciliation and measured request counts
# ---------------------------------------------------------------------------

SUMMARY = {
    "seeds": r"IDs: seeds ([\d,]+)",
    "linked": r"\+ linked ([\d,]+)",
    "pruned": r"= pruned ([\d,]+)",
    "fetched": r"\+ fetched ([\d,]+)",
    "queued": r"\+ queued ([\d,]+)",
    "visited": r"= visited ([\d,]+)",
    "omitted": r"\+ omitted ([\d,]+)",
    "exported": r"exported nodes: ([\d,]+|-)",
    "saved_query": r"saved query ([\d,]+)",
    "wiql_filter": r"WIQL filter calls ([\d,]+)",
    "batch_fetch": r"work-item batches ([\d,]+)",
}


def summary_counts(out):
    counts = {}
    for key, pattern in SUMMARY.items():
        value = re.search(pattern, out).group(1)
        counts[key] = None if value == "-" else int(value.replace(",", ""))
    return counts


def counting_client(items, seeds, rejected=()):
    """fake_client that also records every ADO call it receives."""
    base, log = fake_client(items, seeds, rejected)
    log["calls"] = collections.Counter()

    class Client(base):
        def run_query_by_id(self, *args):
            log["calls"]["saved_query"] += 1
            return super().run_query_by_id(*args)

        def filter_ids_via_wiql(self, *args):
            log["calls"]["wiql_filter"] += 1
            return super().filter_ids_via_wiql(*args)

        def get_work_items_batch(self, ids):
            log["calls"]["batch_fetch"] += 1
            return super().get_work_items_batch(ids)

    return Client, log


def chain(n, fan=3):
    """Items 1..n where each item links to the next `fan` IDs (some beyond n)."""
    return {
        i: wi(i, "Task" if i % 5 == 0 else "Feature", range(i + 1, i + 1 + fan))
        for i in range(1, n + 1)
    }


RECONCILE_CASES = {
    "complete": (dict(items={1: wi(1, targets=[2]), 2: wi(2)}, seeds=[1]), ()),
    "stop-pruned": (
        dict(items=chain(40), seeds=[1, 2], rejected={5, 10, 15}),
        ("--skip-types", "Task"),
    ),
    "stop-fallback": (dict(items=chain(40), seeds=[1, 2]), ("--skip-types", "Task")),
    "follow": (
        dict(items=chain(40), seeds=[1, 2]),
        ("--skip-types", "Task", "--filtered-relations", "follow"),
    ),
    "omitted-linked": (
        dict(items={1: wi(1, targets=[2, 3]), 3: wi(3, targets=[1, 4])}, seeds=[1]),
        (),
    ),
    "failed-seed": (dict(items=chain(10), seeds=[1, 99, 2]), ("--batch-size", "1")),
}


@pytest.mark.parametrize("case", RECONCILE_CASES)
def test_counters_reconcile_with_observed_ids_and_requests(tmp_path, monkeypatch, capsys, case):
    """AG-011: every counted ID and request matches what the fake ADO saw."""
    kwargs, extra = RECONCILE_CASES[case]
    fake, log = counting_client(**kwargs)
    try:
        run_export(monkeypatch, tmp_path, fake, "--batch-size", "7", *extra)
    except SystemExit as exc:
        assert exc.code in (export.EXIT_INCOMPLETE, export.EXIT_SEEDS_MISSING)
    out = capsys.readouterr().out
    counts = summary_counts(out)
    assert "Counters reconcile: yes" in out
    assert counts["seeds"] == len(set(kwargs["seeds"]))
    assert counts["fetched"] == len(log["fetched"]) == len(set(log["fetched"]))
    assert counts["visited"] == len([i for i in log["fetched"] if i in kwargs["items"]])
    assert counts["omitted"] == len([i for i in log["fetched"] if i not in kwargs["items"]])
    assert counts["pruned"] == len(set(log["filtered"]) & set(kwargs.get("rejected", ())))
    assert counts["seeds"] + counts["linked"] == (
        counts["pruned"] + counts["fetched"] + counts["queued"]
    )
    for kind in ("saved_query", "wiql_filter", "batch_fetch"):
        assert counts[kind] == log["calls"][kind], kind
    if (tmp_path / "nodes.parquet").exists():
        assert counts["exported"] == len(pd.read_parquet(tmp_path / "nodes.parquet"))
    else:
        assert counts["exported"] is None and counts["queued"] > 0


def test_unreconciled_counters_are_reported():
    stats = export.ExportStats(frozenset({1, 2}))
    stats.fetched = 1
    stats.record_added("Feature")
    stats.exported = 1
    assert stats.reconcile() == ["seeds + linked = pruned + fetched + queued (2 != 1)"]
    stats.exported = 2
    assert stats.reconcile(queue_remaining=1) == ["exported = added (2 != 1)"]


@pytest.mark.parametrize(
    ("mode", "requests"),
    [("stop", (1, 2, 20)), ("follow", (1, 0, 20))],
)
def test_documented_request_counts_for_two_thousand_seeds(
    tmp_path, monkeypatch, capsys, mode, requests
):
    """AG-011: the spec's worked example (2,000 seeds, batch 100, one Task)."""
    items = {i: wi(i, "Task" if i == 7 else "Feature") for i in range(1, 2001)}
    fake, log = counting_client(items, range(1, 2001), rejected={7})
    run_export(
        monkeypatch,
        tmp_path,
        fake,
        "--batch-size",
        "100",
        "--skip-types",
        "Task",
        "--filtered-relations",
        mode,
    )
    calls = log["calls"]
    assert (calls["saved_query"], calls["wiql_filter"], calls["batch_fetch"]) == requests
    counts = summary_counts(capsys.readouterr().out)
    assert (counts["pruned"], counts["fetched"], counts["exported"]) == (
        (1, 1999, 1999) if mode == "stop" else (0, 2000, 2000)
    )


def test_documented_wiql_requests_per_filter_call(tmp_path, monkeypatch, capsys):
    """AG-011: one filter call is one WIQL query, plus one existence query on a rejection."""
    items = {1: wi(1, targets=[2, 3, 4]), 2: wi(2, "Task"), 4: wi(4, targets=[5]), 5: wi(5)}
    log = {"wiql": [], "fetched": []}
    with pytest.raises(SystemExit):
        run_export(monkeypatch, tmp_path, wiql_client(items, [1], log), "--skip-types", "Task")
    counts = summary_counts(capsys.readouterr().out)
    # Filter calls: seed [1] (accepted), then [2, 3, 4] (2 and 3 rejected), then [5].
    assert counts["wiql_filter"] == 3
    assert [filtered for filtered, _ in log["wiql"]] == [True, True, False, True]
    assert log["batches"] == [[1], [3, 4], [5]] and counts["batch_fetch"] == 3
    assert (counts["pruned"], counts["omitted"], counts["exported"]) == (1, 1, 3)
