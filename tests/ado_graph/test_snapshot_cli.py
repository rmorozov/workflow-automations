"""Offline tests for export --save-snapshot and the replay command."""

import json
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime
from importlib.metadata import version
from types import SimpleNamespace

import pandas as pd
import pytest
from ado_graph_data import fake_client

from workflow_automations import ado_graph as tool
from workflow_automations import ado_triage as triage
from workflow_automations.ado_common.snapshot import load_snapshot
from workflow_automations.ado_graph import client, export

BASE = "https://ado.example/tfs/Collection"
HIERARCHY = "System.LinkTypes.Hierarchy-Forward"
BLOCKS = "Microsoft.VSTS.Common.Blocks-Forward"


def sdk_wi(wi_id, wi_type="Feature", project="Example", links=()):
    return SimpleNamespace(
        id=wi_id,
        fields={
            "System.Title": f"Item {wi_id}",
            "System.WorkItemType": wi_type,
            "System.TeamProject": project,
            "System.AssignedTo": {"displayName": "Owner", "uniqueName": "owner@example"},
            "System.CreatedDate": "2026-09-01T10:00:00.123Z",
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


def export_argv(out, *extra):
    return [
        "export",
        "--ado-url",
        BASE,
        "--project",
        "Example",
        "--query-id",
        "q-guid",
        "--output-dir",
        str(out),
        "--formats",
        "parquet",
        *extra,
    ]


def run(argv, expected_exit=None):
    if expected_exit is None:
        tool.main(argv)
        return
    with pytest.raises(SystemExit) as exit_info:
        tool.main(argv)
    assert exit_info.value.code == expected_exit


def use_client(monkeypatch, items=GRAPH, seeds=SEEDS, rejected=()):
    fake, log = fake_client(items, seeds, rejected)
    monkeypatch.setattr(export, "ADOClient", fake)
    monkeypatch.setattr(export, "ca_bundle", lambda _dir: nullcontext())
    return log


def read_export(out):
    return pd.read_parquet(out / "nodes.parquet"), pd.read_parquet(out / "edges.parquet")


def replay_argv(snapshot, out, *extra):
    return [
        "replay",
        "--snapshot",
        str(snapshot),
        "--output-dir",
        str(out),
        "--formats",
        "parquet",
        *extra,
    ]


def assert_same_export(left, right):
    for left_frame, right_frame in zip(read_export(left), read_export(right), strict=True):
        pd.testing.assert_frame_equal(left_frame, right_frame)


@pytest.mark.parametrize(
    ("extra", "rejected"),
    [
        ([], ()),
        (["--skip-types", ""], ()),
        # Item 4 passes WIQL (as after a failed chunk), is fetched, then dropped.
        (["--include-projects", "example"], ()),
        (["--include-projects", "example"], (2,)),
        (["--filtered-relations", "follow", "--include-projects", "example"], ()),
    ],
)
def test_saved_snapshot_replays_into_the_same_export(monkeypatch, tmp_path, extra, rejected):
    live, replayed, path = tmp_path / "live", tmp_path / "replayed", tmp_path / "snap.json"
    use_client(monkeypatch, rejected=rejected)
    # Item 5 is gone: the export and the replay both exit 4, incomplete.
    run(export_argv(live, "--save-snapshot", str(path), *extra), export.EXIT_INCOMPLETE)

    monkeypatch.setattr(export, "ADOClient", None)  # offline from here
    run(replay_argv(path, replayed), export.EXIT_INCOMPLETE)
    assert_same_export(live, replayed)


def test_snapshot_records_provenance(monkeypatch, tmp_path):
    path = tmp_path / "snap.json"
    use_client(monkeypatch)
    run(
        export_argv(
            tmp_path / "out",
            "--save-snapshot",
            str(path),
            "--include-projects",
            "Example",
            "--skip-types",
            "Task",
        ),
        export.EXIT_INCOMPLETE,
    )
    snap = load_snapshot(path)
    assert snap.producer == "ado-graph"
    assert snap.source == {"base_url": BASE, "project": "Example", "query_id": "q-guid"}
    assert snap.filters == {"skip_types": ["task"], "include_projects": ["example"]}
    assert snap.options == {
        "filtered_relations": "stop",
        "tool_version": version("workflow-automations"),
        "completeness": "incomplete",
    }
    assert snap.captured_at.endswith("+00:00")
    assert snap.roots == SEEDS
    assert snap.omitted == [5]
    # Every fetched item keeps every field as ADO returned it, identities included.
    assert snap.items[0]["fields"] == GRAPH[1].fields


def test_complete_export_replays_with_exit_zero(monkeypatch, tmp_path):
    path = tmp_path / "snap.json"
    items = {1: sdk_wi(1, links=[(HIERARCHY, 3)]), 3: sdk_wi(3)}
    use_client(monkeypatch, items, [1])
    run(export_argv(tmp_path / "live", "--save-snapshot", str(path)))
    assert load_snapshot(path).options["completeness"] == "complete"
    run(replay_argv(path, tmp_path / "replayed"))
    assert_same_export(tmp_path / "live", tmp_path / "replayed")


def test_snapshot_without_provenance_options_still_replays(monkeypatch, tmp_path):
    """A version-1 snapshot saved before the provenance options (SH-004) replays."""
    path = tmp_path / "snap.json"
    use_client(monkeypatch)
    run(export_argv(tmp_path / "live", "--save-snapshot", str(path)), export.EXIT_INCOMPLETE)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["options"] = {"filtered_relations": "stop"}
    path.write_text(json.dumps(doc), encoding="utf-8")
    run(replay_argv(path, tmp_path / "replayed"), export.EXIT_INCOMPLETE)
    assert_same_export(tmp_path / "live", tmp_path / "replayed")


def test_one_snapshot_feeds_graph_and_triage_offline(monkeypatch, tmp_path):
    path = tmp_path / "snap.json"
    use_client(monkeypatch)
    run(
        export_argv(tmp_path / "live", "--save-snapshot", str(path), "--skip-types", ""),
        export.EXIT_INCOMPLETE,
    )
    monkeypatch.setattr(export, "ADOClient", None)
    run(replay_argv(path, tmp_path / "replayed"), export.EXIT_INCOMPLETE)
    assert set(read_export(tmp_path / "replayed")[0]["WI_ID"]) == {1, 2, 3, 4, 6}
    scope = triage.snapshot_from_raw(load_snapshot(path))
    assert [item["id"] for item in scope.items] == SEEDS
    cfg = triage.Settings(as_of=datetime(2026, 10, 1), base_url=BASE)
    master = triage.build_master(scope, {}, cfg)
    assert len(master) == len(SEEDS)
    assert not triage.build_relations(scope, cfg).empty


def test_snapshot_created_during_the_export_is_replaced(monkeypatch, tmp_path):
    """Consent is checked when the run starts, as for the export files."""
    path = tmp_path / "snap.json"
    fake, _ = fake_client(GRAPH, SEEDS)

    class Racing(fake):
        def run_query_by_id(self, project, query):
            path.write_text("created meanwhile", encoding="utf-8")
            return super().run_query_by_id(project, query)

    monkeypatch.setattr(export, "ADOClient", Racing)
    monkeypatch.setattr(export, "ca_bundle", lambda _dir: nullcontext())
    run(export_argv(tmp_path / "out", "--save-snapshot", str(path)), export.EXIT_INCOMPLETE)
    assert load_snapshot(path).roots == SEEDS


def test_failed_snapshot_still_writes_the_export(monkeypatch, tmp_path):
    use_client(monkeypatch)

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(export, "save_snapshot", fail)
    out = tmp_path / "out"
    run(
        export_argv(out, "--save-snapshot", str(tmp_path / "snap.json")),
        export.EXIT_SNAPSHOT_FAILED,
    )
    assert set(read_export(out)[0]["WI_ID"]) == {1, 4, 6}
    assert not (tmp_path / "snap.json").exists()


def test_snapshot_directory_is_refused_before_fetching(monkeypatch, tmp_path):
    log = use_client(monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        tool.main(export_argv(tmp_path / "out", "--save-snapshot", str(tmp_path), "--overwrite"))
    assert exit_info.value.code == 2
    assert log["fetched"] == []


def test_existing_snapshot_is_refused_before_fetching(monkeypatch, tmp_path):
    path = tmp_path / "snap.json"
    path.write_text("keep", encoding="utf-8")
    log = use_client(monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        tool.main(export_argv(tmp_path / "out", "--save-snapshot", str(path)))
    assert exit_info.value.code == 2
    assert log["fetched"] == []
    assert path.read_text(encoding="utf-8") == "keep"

    run(
        export_argv(tmp_path / "out", "--save-snapshot", str(path), "--overwrite"),
        export.EXIT_INCOMPLETE,
    )
    assert load_snapshot(path).roots == SEEDS


def test_snapshot_cannot_be_an_export_file(monkeypatch, tmp_path):
    log = use_client(monkeypatch)
    snapshot = tmp_path / "out" / "nodes.parquet"
    with pytest.raises(SystemExit) as exit_info:
        tool.main(export_argv(tmp_path / "out", "--save-snapshot", str(snapshot), "--overwrite"))
    assert exit_info.value.code == 2
    assert log["fetched"] == []


def test_missing_seed_writes_no_snapshot(monkeypatch, tmp_path):
    path = tmp_path / "snap.json"
    use_client(monkeypatch, seeds=[1, 7])
    run(export_argv(tmp_path / "out", "--save-snapshot", str(path)), export.EXIT_SEEDS_MISSING)
    assert not path.exists()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "not a JSON snapshot"),
        (json.dumps({"schema": "other"}), "not an ADO snapshot"),
        (
            json.dumps(triage.snapshot_to_raw(triage.Snapshot(items=[]), BASE, "q").to_json()),
            "ado-graph replays only ado-graph snapshots",
        ),
    ],
)
def test_replay_refuses_what_is_not_a_graph_snapshot(tmp_path, capsys, content, message):
    path = tmp_path / "snap.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        tool.main(replay_argv(path, tmp_path / "out"))
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out" / "nodes.parquet").exists()


def test_replay_refuses_existing_outputs_without_overwrite(monkeypatch, tmp_path):
    path = tmp_path / "snap.json"
    use_client(monkeypatch)
    run(export_argv(tmp_path / "out", "--save-snapshot", str(path)), export.EXIT_INCOMPLETE)
    with pytest.raises(SystemExit) as exit_info:
        tool.main(replay_argv(path, tmp_path / "out"))
    assert exit_info.value.code == 2
    run(replay_argv(path, tmp_path / "out", "--overwrite"), export.EXIT_INCOMPLETE)


OFFLINE_REPLAY = """
import sys

for name in BLOCKED:
    sys.modules[name] = None  # any import of it raises ImportError

from workflow_automations import ado_graph

try:
    ado_graph.main(["replay", "--snapshot", sys.argv[1], "--output-dir", sys.argv[2],
                    "--formats", "parquet"])
except SystemExit as exc:
    assert exc.code == 4, exc.code
"""


def test_replay_runs_without_network_extras(monkeypatch, tmp_path):
    path = tmp_path / "snap.json"
    use_client(monkeypatch)
    run(export_argv(tmp_path / "live", "--save-snapshot", str(path)), export.EXIT_INCOMPLETE)
    blocked = (*client.NETWORK_MODULES, "azure", "msrest", "requests_kerberos", "tqdm")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"BLOCKED = {blocked!r}\n{OFFLINE_REPLAY}",
            str(path),
            str(tmp_path / "replayed"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert_same_export(tmp_path / "live", tmp_path / "replayed")
