"""Offline behavior tests for the ado-graph analysis workbook."""

import datetime as dt
import os
import time

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

    def fail(_results, path, *_rest):
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


@pytest.mark.parametrize(
    ("frame", "fits"),
    [
        (pd.DataFrame({"a": range(report.EXCEL_MAX_ROWS - 1)}), True),
        (pd.DataFrame({"a": range(report.EXCEL_MAX_ROWS)}), False),
        (pd.DataFrame(columns=range(report.EXCEL_MAX_COLUMNS)), True),
        (pd.DataFrame(columns=range(report.EXCEL_MAX_COLUMNS + 1)), False),
        (pd.DataFrame({"a": ["x" * report.EXCEL_MAX_CELL_CHARS, None]}), True),
        (pd.DataFrame({"a": ["x" * (report.EXCEL_MAX_CELL_CHARS + 1), 1]}), False),
        (
            pd.DataFrame(
                {"a": pd.Series(["x" * (report.EXCEL_MAX_CELL_CHARS + 1)], dtype="string")}
            ),
            False,
        ),
        (
            pd.DataFrame(
                {"a": pd.Series([1, dt.datetime(2024, 1, 1), [0] * 40_000], dtype=object)}
            ),
            True,
        ),
    ],
    ids=[
        "max-rows",
        "rows+1",
        "max-columns",
        "columns+1",
        "max-cell",
        "cell+1",
        "string-dtype",
        "no-text",
    ],
)
def test_excel_limits_at_their_boundaries(frame, fits):
    assert (report.excel_overflow(frame) is None) is fits


def test_oversized_analysis_sheet_spills_to_csv_with_a_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "EXCEL_MAX_ROWS", 4)
    big = pd.DataFrame({"CR_ID": range(4), "Reachable_ID": range(10, 14)})
    small = pd.DataFrame({"CR_ID": [1, 2, 3]})
    output = tmp_path / "report.xlsx"
    report.write_analysis_report({"cr_impact_unpacked": big, "orphans": small}, output)

    spill = tmp_path / "report.cr_impact_unpacked.csv"
    assert pd.read_csv(spill).equals(big)
    workbook = pd.read_excel(output, sheet_name=None)
    assert list(workbook) == ["cr_impact_unpacked", "orphans"]
    pointer = workbook["cr_impact_unpacked"].iloc[0]
    assert pointer["Spilled_To"] == spill.name
    assert (pointer["Rows"], pointer["Columns"]) == (4, 2)
    assert "exceed Excel's 4" in pointer["Reason"]
    assert workbook["orphans"].equals(small)


def test_failed_spill_keeps_the_previous_report_and_spill(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "EXCEL_MAX_ROWS", 2)
    output = tmp_path / "report.xlsx"
    spill = tmp_path / "report.cycles.csv"
    output.write_bytes(b"previous report")
    spill.write_bytes(b"previous spill")

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(report, "_write_workbook", fail)
    with pytest.raises(OSError, match="disk full"):
        report.write_analysis_report({"cycles": pd.DataFrame({"a": range(5)})}, output)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {
        "report.xlsx": b"previous report",
        "report.cycles.csv": b"previous spill",
    }


def test_export_over_excel_limits_fails_before_replacing_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "EXCEL_MAX_ROWS", 3)
    (tmp_path / "nodes.csv").write_bytes(b"previous")
    with pytest.raises(report.ExcelLimitError, match="sheet Nodes: 5 rows.*--formats parquet"):
        report._export_dataframes(nodes(), edges(), tmp_path, ["csv", "excel"])
    assert [p.name for p in tmp_path.iterdir()] == ["nodes.csv"]
    assert (tmp_path / "nodes.csv").read_bytes() == b"previous"

    report._export_dataframes(nodes(), edges(), tmp_path, ["csv"])
    assert pd.read_csv(tmp_path / "nodes.csv")["WI_ID"].tolist() == [1, 2, 3, 4]


def test_column_widths_sample_rows_and_cap_at_fifty(monkeypatch):
    monkeypatch.setattr(report, "SIZING_SAMPLE_ROWS", 3)
    titles = ["first"] + ["x" * 30] + ["y"] * 7 + ["last-row"]
    frame = pd.DataFrame({"Title": titles, "Description": ["z" * 80] * 10})
    # Rows 0, 4 and 9 are sampled: the 30-character row 1 is not.
    assert report._column_widths("s", frame) == [len("last-row") + 2, 50]
    assert report._sample_rows(frame).index.tolist() == [0, 4, 9]


def test_unsizable_column_is_reported_and_keeps_default_width(caplog):
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no text")

    frame = pd.DataFrame({"Bad": [Unprintable()], "Good": ["ok"]})
    assert report._column_widths("cycles", frame) == [None, len("Good") + 2]
    assert "Sheet 'cycles': cannot size column 'Bad' (no text)" in caplog.text


def test_long_step_names_are_truncated_sheets_with_widths(tmp_path):
    name = "cr_prereqs_unpacked@hierarchy_only"
    output = tmp_path / "report.xlsx"
    report.write_analysis_report({name: pd.DataFrame({"Title": ["a" * 20]})}, output)
    workbook = load_workbook(output)
    try:
        sheet = workbook[name[:31]]
        assert sheet.column_dimensions["A"].width == 22
    finally:
        workbook.close()


@pytest.mark.skipif(
    not os.environ.get("ADO_GRAPH_BENCH"), reason="set ADO_GRAPH_BENCH=1 to run the benchmark"
)
def test_large_report_benchmark(tmp_path):
    """Print graph compute, writing and sizing time for a large synthetic report."""
    count = int(os.environ.get("ADO_GRAPH_BENCH_NODES", "5000"))
    bench_nodes = pd.DataFrame(
        {
            "WI_ID": range(1, count + 1),
            "Title": [f"Item {i}" for i in range(1, count + 1)],
            "WorkItemType": ["Change Request" if i % 50 == 1 else "Feature" for i in range(count)],
            "Effort": 1,
        }
    )
    bench_edges = pd.DataFrame(
        [(i, i + 1, "Successor->Predecessor") for i in range(1, count)],
        columns=["Source_ID", "Target_ID", "Link_Type"],
    )
    start = time.perf_counter()
    analyzer = analyze.GraphAnalyzer(bench_nodes, bench_edges, effort_field="Effort")
    results = {
        "orphans": analyzer.find_orphans(subgraph=None),
        "metrics": analyzer.compute_metrics(subgraph=None),
        "cr_impact_unpacked": analyzer.analyze_cr_impact_unpacked(subgraph=None),
    }
    compute = time.perf_counter() - start
    timings: dict[str, float] = {}
    report.write_analysis_report(results, tmp_path / "report.xlsx", timings=timings)
    rows = sum(len(frame) for frame in results.values())
    print(
        f"\n{count} nodes, {rows} report rows: compute {compute:.2f}s, "
        f"write {timings['write']:.2f}s, sizing {timings['sizing']:.2f}s"
    )
    assert timings["sizing"] < timings["write"]
