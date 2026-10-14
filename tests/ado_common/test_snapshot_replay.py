"""Snapshots saved by each ADO tool replay offline into that tool's output."""

from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from workflow_automations import ado_graph
from workflow_automations import ado_triage as triage
from workflow_automations.ado_common.snapshot import SnapshotError, load_snapshot, save_snapshot
from workflow_automations.ado_graph import export
from workflow_automations.ado_graph import snapshot as graph_snapshot

AS_OF = datetime(2026, 10, 1)
BASE = "https://ado.example/tfs/Collection"
HIERARCHY = "System.LinkTypes.Hierarchy-Forward"
BLOCKS = "Microsoft.VSTS.Common.Blocks-Forward"


# ---------------------------------------------------------------------------
# ado-graph
# ---------------------------------------------------------------------------


def sdk_wi(wi_id, wi_type="Feature", project="Example", links=()):
    return SimpleNamespace(
        id=wi_id,
        fields={
            "System.Title": f"Item {wi_id}",
            "System.WorkItemType": wi_type,
            "System.TeamProject": project,
            "System.State": "Active",
        },
        relations=[
            SimpleNamespace(rel=rel, url=f"{BASE}/_apis/wit/workItems/{t}") for rel, t in links
        ],
    )


# 1 -> 2 (Task bridge) -> 3; 1 -> 4 (other project); 1 -> 5 (deleted); 6 is a seed.
GRAPH = {
    1: sdk_wi(1, links=[(HIERARCHY, 2), (BLOCKS, 4), (BLOCKS, 5)]),
    2: sdk_wi(2, "Task", links=[(BLOCKS, 3)]),
    3: sdk_wi(3),
    4: sdk_wi(4, project="Elsewhere"),
    6: sdk_wi(6, "Epic", links=[(HIERARCHY, 1)]),
}
SEEDS = [1, 6]


def export_and_capture(monkeypatch, out, *extra):
    """Run the export CLI on fakes; return the snapshot of what it fetched."""
    fetched, omitted = [], []

    class Client:
        def __init__(self, _url, ca_bundle=None):
            pass

        def run_query_by_id(self, _project, _query):
            return list(SEEDS)

        def filter_ids_via_wiql(self, ids, _projects, _types):
            return set(ids)  # as after a failed WIQL chunk: fetch, then filter

        def get_work_items_batch(self, ids):
            items = [GRAPH[i] for i in ids if i in GRAPH]
            fetched.extend(items)
            omitted.extend(i for i in ids if i not in GRAPH)
            return items

    monkeypatch.setattr(export, "ADOClient", Client)
    monkeypatch.setattr(export, "ca_bundle", lambda _dir: nullcontext())
    argv = ["export", "--ado-url", BASE, "--query-id", "q", "--output-dir", str(out)]
    with pytest.raises(SystemExit) as exit_info:  # 5 is omitted: exit 4, incomplete
        ado_graph.main([*argv, "--formats", "parquet", *extra])
    assert exit_info.value.code == export.EXIT_INCOMPLETE
    args = ado_graph.cli.build_parser().parse_args([*argv, *extra])
    return graph_snapshot.snapshot_from_work_items(
        fetched,
        SEEDS,
        base_url=BASE,
        project="",
        query_id="q",
        skip_types=export._parse_skip_types(args.skip_types),
        include_projects=export._parse_include_projects(args.include_projects),
        filtered_relations=args.filtered_relations,
        omitted=omitted,
        captured_at="2026-10-14T00:00:00+00:00",
    )


def read_export(out):
    return pd.read_parquet(out / "nodes.parquet"), pd.read_parquet(out / "edges.parquet")


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--skip-types", ""],
        ["--include-projects", "example"],
        ["--filtered-relations", "follow", "--include-projects", "example"],
    ],
)
def test_graph_snapshot_replays_into_the_same_export_and_report(monkeypatch, tmp_path, extra):
    live, replayed = tmp_path / "live", tmp_path / "replayed"
    snap = export_and_capture(monkeypatch, live, *extra)
    path = tmp_path / "graph.json"
    save_snapshot(snap, path)

    # Offline from here: any client use would fail.
    monkeypatch.setattr(export, "ADOClient", None)
    # Item 5 is gone: the live export exits 4, the replay returns it.
    assert graph_snapshot.replay_export(load_snapshot(path), replayed, ["parquet"]) == [5]
    for live_frame, replayed_frame in zip(read_export(live), read_export(replayed), strict=True):
        pd.testing.assert_frame_equal(live_frame, replayed_frame)

    reports = []
    for out in (live, replayed):
        report = out / "report.xlsx"
        ado_graph.main(
            [
                "analyze",
                "--nodes-file",
                str(out / "nodes.parquet"),
                "--edges-file",
                str(out / "edges.parquet"),
                "--output-file",
                str(report),
            ]
        )
        reports.append(pd.read_excel(report, sheet_name=None))
    assert reports[0].keys() == reports[1].keys()
    for sheet, frame in reports[0].items():
        pd.testing.assert_frame_equal(frame, reports[1][sheet])


def test_graph_replay_refuses_what_export_would_not_write(monkeypatch, tmp_path):
    snap = export_and_capture(monkeypatch, tmp_path / "live")
    snap.omitted.append(6)
    with pytest.raises(SnapshotError, match=r"saved-query work items were not returned: \[6\]"):
        graph_snapshot.frames_from_snapshot(snap)
    triage_raw = triage.snapshot_to_raw(triage.Snapshot(items=[]), BASE, "q")
    with pytest.raises(SnapshotError, match="ado-graph replays only ado-graph snapshots"):
        graph_snapshot.frames_from_snapshot(triage_raw)


def test_existing_replay_outputs_need_overwrite(monkeypatch, tmp_path):
    out = tmp_path / "live"
    snap = export_and_capture(monkeypatch, out)
    with pytest.raises(FileExistsError):
        graph_snapshot.replay_export(snap, out, ["parquet"])
    graph_snapshot.replay_export(snap, out, ["parquet"], overwrite=True)


# ---------------------------------------------------------------------------
# ado-triage
# ---------------------------------------------------------------------------


def triage_item(wi_id, title, *, rw=0, parent=None, relations=()):
    fields = {
        triage.FIELD_TITLE: title,
        triage.FIELD_AREA_PATH: "Proj\\Team",
        triage.FIELD_STATE: "Active",
        triage.FIELD_REMAINING_WORK: rw,
        triage.FIELD_CHANGED_DATE: (AS_OF - timedelta(days=30)).isoformat() + "Z",
        triage.FIELD_ASSIGNED_TO: {"displayName": "Owner"},
    }
    if parent is not None:
        fields[triage.FIELD_PARENT] = parent
    links = [{"rel": rel, "url": f"{BASE}/_apis/wit/workItems/{t}"} for rel, t in relations]
    return {"id": wi_id, "fields": fields, "relations": links}


def triage_snapshot():
    return triage.Snapshot(
        items=[
            triage_item(1, "Root", rw=40, relations=[(triage.REL_BLOCKS_FORWARD, 2)]),
            triage_item(2, "Blocked", rw=8, parent=1, relations=[(triage.REL_PREDECESSOR, 9)]),
            triage_item(3, "Orphan child", parent=8),
        ],
        external={9: {triage.FIELD_TITLE: "Outside", triage.FIELD_AREA_PATH: "Other\\Team"}},
        parent_titles={1: "Root", 8: "Outside parent"},
        warnings=["1 linked items unavailable (deleted or no permission)"],
    )


def settings():
    return triage.Settings(as_of=AS_OF, base_url=BASE)


def assert_same_report(left, right):
    assert left.sheets.keys() == right.sheets.keys()
    for name, frame in left.sheets.items():
        pd.testing.assert_frame_equal(frame, right.sheets[name])
    pd.testing.assert_frame_equal(left.relations, right.relations)
    assert (left.omitted, left.notes) == (right.omitted, right.notes)


def test_triage_snapshot_replays_into_the_same_report(tmp_path):
    original = triage_snapshot()
    path = tmp_path / "triage.json"
    save_snapshot(triage.snapshot_to_raw(original, BASE, "q"), path)

    restored = triage.snapshot_from_raw(load_snapshot(path))
    assert restored == original
    assert_same_report(
        triage.build_report(original, {}, settings()),
        triage.build_report(restored, {}, settings()),
    )


@pytest.mark.parametrize(
    ("external", "parent_titles"),
    [
        ({}, {9: "Fetched only as a parent"}),
        ({9: {triage.FIELD_TITLE: "Fetched only as linked"}}, {}),
        ({9: {triage.FIELD_TITLE: "Linked"}}, {9: "Parent"}),
    ],
)
def test_triage_keeps_linked_items_and_parents_apart(tmp_path, external, parent_titles):
    # 9 is both the parent and the predecessor of 1; either fetch may have failed.
    item = triage_item(1, "Child", parent=9, relations=[(triage.REL_PREDECESSOR, 9)])
    item["relations"].append({"rel": None, "url": None})  # as the SDK can return it
    original = triage.Snapshot(items=[item], external=external, parent_titles=parent_titles)
    path = tmp_path / "triage.json"
    save_snapshot(triage.snapshot_to_raw(original, BASE, "q"), path)
    restored = triage.snapshot_from_raw(load_snapshot(path))
    assert (restored.external, restored.parent_titles) == (external, parent_titles)
    assert_same_report(
        triage.build_report(original, {}, settings()),
        triage.build_report(restored, {}, settings()),
    )


def test_filtered_graph_snapshot_is_not_a_triage_scope(monkeypatch, tmp_path):
    snap = export_and_capture(monkeypatch, tmp_path / "live", "--include-projects", "example")
    with pytest.raises(triage.TriageError, match=r"filtered \(skip_types, include_projects\)"):
        triage.snapshot_from_raw(snap)


def test_unfiltered_graph_snapshot_gives_the_query_scope(monkeypatch, tmp_path):
    snap = export_and_capture(monkeypatch, tmp_path / "live", "--skip-types", "")
    scope = triage.snapshot_from_raw(snap)
    # Only the saved query's items are triaged; walked items label relations.
    assert [item["id"] for item in scope.items] == [1, 6]
    assert sorted(scope.external) == [2, 4]
    assert scope.warnings == ["1 linked items unavailable (not in the snapshot)"]
    rows = triage.build_report(scope, {}, settings()).relations
    assert rows.set_index(triage.REL_COL_DEST_ID).loc[2, triage.REL_COL_DEST_TITLE] == "Item 2"


def test_triage_refuses_a_snapshot_missing_queried_items(monkeypatch, tmp_path):
    snap = export_and_capture(monkeypatch, tmp_path / "live", "--skip-types", "")
    snap.roots.append(5)
    with pytest.raises(triage.TriageError, match=r"1 queried work items are missing.*\[5\]"):
        triage.snapshot_from_raw(snap)
