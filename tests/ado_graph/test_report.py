"""Offline behavior tests for the ado-graph analysis workbook."""

import datetime as dt

import pandas as pd
import pytest
from ado_graph_data import edges, fake_client, nodes, run_export, wi
from openpyxl import load_workbook

from workflow_automations import ado_graph as tool
from workflow_automations.ado_graph import analyze, report


def write_inputs(tmp_path):
    node_file, edge_file = tmp_path / "nodes.parquet", tmp_path / "edges.parquet"
    nodes().to_parquet(node_file, index=False)
    edges().to_parquet(edge_file, index=False)
    return node_file, edge_file


def run_analyze(node_file, edge_file, output, *extra):
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
            "--steps",
            "orphans",
            *extra,
        ]
    )


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
        assert workbook.sheetnames == list(analyze.ALL_STEPS)
        assert workbook["orphans"]["A2"].value == 4
        assert workbook["cr_scope_impact"]["C2"].value == 5
    finally:
        workbook.close()


def test_analyze_refuses_existing_report_without_overwrite(tmp_path, capsys):
    node_file, edge_file = write_inputs(tmp_path)
    output = tmp_path / "report.xlsx"
    output.write_bytes(b"keep me")
    with pytest.raises(SystemExit) as exit_info:
        run_analyze(node_file, edge_file, output)
    assert exit_info.value.code == 2
    assert "--overwrite" in capsys.readouterr().err
    assert output.read_bytes() == b"keep me"

    run_analyze(node_file, edge_file, output, "--overwrite")
    workbook = load_workbook(output)
    try:
        assert workbook.sheetnames == ["orphans"]
    finally:
        workbook.close()


@pytest.mark.parametrize("which", ["nodes", "edges"])
def test_analyze_never_writes_over_an_input(tmp_path, which):
    node_file, edge_file = write_inputs(tmp_path)
    target = node_file if which == "nodes" else edge_file
    before = target.read_bytes()
    alias = tmp_path / "alias.parquet"
    alias.symlink_to(target)
    for output in (target, alias):
        with pytest.raises(SystemExit) as exit_info:
            run_analyze(node_file, edge_file, output, "--overwrite")
        assert exit_info.value.code == 2
    assert target.read_bytes() == before


def test_failed_analysis_write_keeps_existing_report(tmp_path, monkeypatch):
    node_file, edge_file = write_inputs(tmp_path)
    output = tmp_path / "report.xlsx"
    output.write_bytes(b"previous report")

    def fail(_results, path):
        path.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(report, "_write_workbook", fail)
    with pytest.raises(OSError, match="disk full"):
        run_analyze(node_file, edge_file, output, "--overwrite")
    assert output.read_bytes() == b"previous report"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "edges.parquet",
        "nodes.parquet",
        "report.xlsx",
    ]


def test_export_refuses_existing_files_before_fetching(tmp_path, monkeypatch, capsys):
    client, log = fake_client({1: wi(1)}, [1])
    (tmp_path / "edges.parquet").write_bytes(b"keep me")
    with pytest.raises(SystemExit) as exit_info:
        run_export(monkeypatch, tmp_path, client)
    assert exit_info.value.code == 2
    assert "edges.parquet exists" in capsys.readouterr().err
    assert log["fetched"] == []
    assert (tmp_path / "edges.parquet").read_bytes() == b"keep me"

    exported, _ = run_export(monkeypatch, tmp_path, client, "--overwrite")
    assert exported["WI_ID"].tolist() == [1]


def test_failed_export_leaves_the_whole_previous_bundle(tmp_path, monkeypatch):
    previous = {name: name.encode() for name in ("nodes.csv", "edges.csv", "work_items.xlsx")}
    for name, content in previous.items():
        (tmp_path / name).write_bytes(content)

    def fail(_nodes, _edges, path):
        path.write_bytes(b"partial")
        raise ValueError("cannot serialize")

    monkeypatch.setattr(report, "_write_excel", fail)
    with pytest.raises(ValueError, match="cannot serialize"):
        report._export_dataframes(nodes(), edges(), tmp_path, ["csv", "excel"])
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == previous


def test_export_publishes_each_format_once_and_skips_unknown(tmp_path):
    report._export_dataframes(nodes(), edges(), tmp_path, ["csv", "bogus", "csv"])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["edges.csv", "nodes.csv"]
    assert report.export_paths(tmp_path, ["excel", "bogus", "excel"]) == [
        tmp_path / "work_items.xlsx"
    ]


def read_bundle(output_dir):
    """Nodes and edges back from every export format, keyed by format."""
    workbook = pd.read_excel(output_dir / "work_items.xlsx", sheet_name=None)
    return {
        "parquet": (
            pd.read_parquet(output_dir / "nodes.parquet"),
            pd.read_parquet(output_dir / "edges.parquet"),
        ),
        "csv": (
            pd.read_csv(output_dir / "nodes.csv", keep_default_na=False, na_values=[""]),
            pd.read_csv(output_dir / "edges.csv"),
        ),
        "excel": (workbook["Nodes"], workbook["Edges"]),
    }


def realistic_nodes():
    jane = {
        "displayName": "Jane Doe",
        "uniqueName": "jane@example.com",
        "id": "00000000-0000-0000-0000-000000000001",
        "_links": {"avatar": {"href": "https://ado.example/avatar"}},
    }
    plus_two = dt.timezone(dt.timedelta(hours=2))
    return pd.DataFrame(
        [
            {
                "WI_ID": 1,
                "Title": '=HYPERLINK("https://evil.example")',
                "System.AssignedTo": jane,
                "System.ChangedDate": "2024-05-01T10:00:00.123Z",
                "Custom.Reviewed": dt.datetime(2024, 5, 1, 12, 0, tzinfo=plus_two),
                "Custom.Tags": ["b", "a"],
                "In_Scope": True,
            },
            {
                "WI_ID": 2,
                "Title": "+1 more",
                "System.AssignedTo": {"displayName": "Build Service"},
                "System.ChangedDate": None,
                "Custom.Reviewed": None,
                "Custom.Tags": None,
                "In_Scope": True,
            },
        ]
    )


def test_export_keeps_text_literal_and_normalizes_identities_and_dates(tmp_path):
    report._export_dataframes(realistic_nodes(), edges(), tmp_path, ["parquet", "csv", "excel"])
    for fmt, (got_nodes, got_edges) in read_bundle(tmp_path).items():
        assert got_nodes["Title"].tolist() == ['=HYPERLINK("https://evil.example")', "+1 more"], fmt
        assert got_nodes["System.AssignedTo"].tolist() == [
            "Jane Doe <jane@example.com>",
            "Build Service",
        ], fmt
        changed = pd.to_datetime(got_nodes["System.ChangedDate"])
        reviewed = pd.to_datetime(got_nodes["Custom.Reviewed"])
        assert changed[0] == pd.Timestamp("2024-05-01 10:00:00.123"), fmt
        assert reviewed[0] == pd.Timestamp("2024-05-01 10:00:00"), fmt
        assert changed.isna()[1] and reviewed.isna()[1], fmt
        assert got_nodes["Custom.Tags"][0] == '["b", "a"]', fmt
        assert got_edges.astype({"Link_Type": str}).equals(edges().astype({"Link_Type": str})), fmt

    workbook = load_workbook(tmp_path / "work_items.xlsx")
    try:
        title = workbook["Nodes"]["B2"]
        assert title.data_type == "s" and title.quotePrefix
    finally:
        workbook.close()


def test_empty_export_round_trips_through_every_format(tmp_path, monkeypatch):
    client, _ = fake_client({1: wi(1, "Task")}, [1])
    run_export(monkeypatch, tmp_path, client, "--formats", "parquet,csv,excel")
    for fmt, (got_nodes, got_edges) in read_bundle(tmp_path).items():
        assert got_nodes.empty and got_edges.empty, fmt
        assert got_nodes.columns.tolist() == ["WI_ID", "In_Scope"], fmt
        assert got_edges.columns.tolist() == ["Source_ID", "Target_ID", "Link_Type"], fmt
    parquet_nodes, parquet_edges = read_bundle(tmp_path)["parquet"]
    assert parquet_nodes["WI_ID"].dtype == "int64"
    assert parquet_edges["Source_ID"].dtype == "int64"


@pytest.mark.parametrize(
    ("bad_nodes", "bad_edges", "message"),
    [
        (nodes().drop(columns="WI_ID"), edges(), "missing column WI_ID"),
        (pd.concat([nodes(), nodes().head(1)]), edges(), r"duplicate WI_ID values \[1\]"),
        (nodes().astype({"WI_ID": float}), edges(), "WI_ID must hold integer"),
        (nodes(), edges().rename(columns={"Link_Type": "Type"}), "edges: columns must be"),
        (nodes(), edges().astype({"Target_ID": str}), "Target_ID must hold integer"),
    ],
)
def test_export_schema_errors_write_nothing(tmp_path, bad_nodes, bad_edges, message):
    (tmp_path / "nodes.csv").write_bytes(b"previous")
    with pytest.raises(ValueError, match=message):
        report._export_dataframes(bad_nodes, bad_edges, tmp_path, ["csv", "excel"])
    assert [p.name for p in tmp_path.iterdir()] == ["nodes.csv"]
    assert (tmp_path / "nodes.csv").read_bytes() == b"previous"
