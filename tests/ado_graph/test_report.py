"""Offline behavior tests for the ado-graph analysis workbook."""

import datetime as dt
import os
import time
from types import SimpleNamespace

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
        assert workbook.sheetnames == ["About", "Config", "Summary", *analyze.ALL_STEPS]
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
        assert workbook.sheetnames == ["About", "Config", "Summary", "orphans"]
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

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    # The workbook is staged first; failing the spill proves both publish together.
    monkeypatch.setattr(pd.DataFrame, "to_csv", fail)
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


def test_repeated_column_names_are_checked_and_sized_by_position(tmp_path):
    # cr_scope_impact without --effort-field repeats Impacted_WI_Count.
    frame = pd.DataFrame([[1, "x" * 12, 3]], columns=["CR_ID", "Count", "Count"])
    assert report.excel_overflow(frame) is None
    assert report._column_widths("s", frame) == [7, 14, 7]
    output = tmp_path / "report.xlsx"
    report.write_analysis_report({"cr_scope_impact": frame}, output)
    assert pd.read_excel(output).shape == (1, 3)


def test_spill_never_replaces_a_file_without_its_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report, "EXCEL_MAX_ROWS", 1)
    node_file, edge_file = write_inputs(tmp_path)
    stray = tmp_path / "report.orphans.csv"
    stray.write_bytes(b"user's own file")
    with pytest.raises(FileExistsError, match="report.orphans.csv exists"):
        run_analyze(node_file, edge_file, tmp_path / "report.xlsx")
    assert stray.read_bytes() == b"user's own file"
    assert not (tmp_path / "report.xlsx").exists()

    # Replacing an existing report (which needs --overwrite) also replaces its spill.
    (tmp_path / "report.xlsx").write_bytes(b"previous report")
    run_analyze(node_file, edge_file, tmp_path / "report.xlsx", "--overwrite")
    assert pd.read_csv(stray)["WI_ID"].tolist() == [4]


def test_categorical_text_counts_toward_the_cell_limit():
    long_text = "x" * (report.EXCEL_MAX_CELL_CHARS + 1)
    frame = pd.DataFrame({"a": pd.Categorical([long_text, "short"])})
    assert "column 'a'" in report.excel_overflow(frame)


def test_write_timing_includes_the_workbook_save(tmp_path, monkeypatch):
    real_writer = pd.ExcelWriter

    def slow_writer(*args, **kwargs):
        writer = real_writer(*args, **kwargs)
        close = writer.close

        def slow_close():
            time.sleep(0.2)
            close()

        writer.close = slow_close
        return writer

    monkeypatch.setattr(pd, "ExcelWriter", slow_writer)
    timings: dict[str, float] = {}
    report.write_analysis_report(
        {"s": pd.DataFrame({"a": [1]})}, tmp_path / "r.xlsx", timings=timings
    )
    assert timings["write"] >= 0.2


def tangled_graph():
    """Two cycles in one component, a parallel link, two CRs and a bridge."""
    graph_nodes = pd.DataFrame(
        [
            {"WI_ID": i, "Title": f"Item {i}", "WorkItemType": kind, "Effort": 1, "In_Scope": True}
            for i, kind in [
                (1, "Change Request"),
                (2, "Feature"),
                (3, "Feature"),
                (4, "Feature"),
                (5, "Change Request"),
                (6, "Feature"),
                (7, "Feature"),
                (8, "Feature"),
            ]
        ]
    )
    graph_nodes.loc[graph_nodes["WI_ID"] == 8, "In_Scope"] = False
    links = pd.DataFrame(
        [
            (1, 2, "Blocks"),
            (2, 3, "Blocks"),
            (3, 2, "Blocks"),
            (3, 4, "Predecessor->Successor"),
            (4, 3, "Blocked By"),
            (4, 2, "Blocks"),
            (5, 6, "Parent->Child"),
            (6, 8, "Affects"),
            (8, 7, "Affects"),
            (1, 5, "Implements"),
        ],
        columns=["Source_ID", "Target_ID", "Link_Type"],
    )
    return graph_nodes, links


def analyze_files(tmp_path, graph_nodes, links, name, *extra):
    node_file, edge_file = tmp_path / f"{name}.nodes.parquet", tmp_path / f"{name}.edges.parquet"
    graph_nodes.to_parquet(node_file, index=False)
    links.to_parquet(edge_file, index=False)
    output = tmp_path / f"{name}.xlsx"
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
            "--ado-url",
            "https://ado.example/Coll/",
            *extra,
        ]
    )
    return output


def cell_values(path):
    workbook = load_workbook(path)
    try:
        return {
            sheet.title: [list(row) for row in sheet.iter_rows(values_only=True)]
            for sheet in workbook
        }
    finally:
        workbook.close()


def test_repeat_runs_on_shuffled_input_rows_give_identical_cells(tmp_path):
    graph_nodes, links = tangled_graph()
    first = cell_values(analyze_files(tmp_path, graph_nodes, links, "first"))
    shuffled = cell_values(
        analyze_files(
            tmp_path,
            graph_nodes.sample(frac=1, random_state=3),
            links.sample(frac=1, random_state=7),
            "second",
        )
    )
    # Only the input file names differ between the two runs.
    for values in (first, shuffled):
        for sheet in ("About", "Config"):
            values[sheet] = [row for row in values[sheet] if "file" not in str(row[0])]
    assert first == shuffled


def test_result_sheets_list_members_in_a_fixed_order(tmp_path):
    graph_nodes, links = tangled_graph()
    sheets = pd.read_excel(analyze_files(tmp_path, graph_nodes, links, "order"), sheet_name=None)
    cycles = sheets["cycles"].groupby("Cycle_Index")["Node_ID"].apply(list).tolist()
    assert cycles == [[2, 3], [2, 3, 4]]
    scc = sheets["scc"]
    assert scc["WI_ID"].tolist()[:3] == [2, 3, 4]  # largest component first
    assert scc.loc[3:, "WI_ID"].tolist() == [1, 5, 6, 7]
    assert sheets["metrics"]["WI_ID"].tolist() == [1, 2, 3, 4, 5, 6, 7]
    assert sheets["orphans"].empty
    unpacked = sheets["cr_impact_unpacked"]
    assert unpacked[["CR_ID", "Impacted_WI_ID"]].values.tolist() == [
        [1, 2],
        [1, 3],
        [1, 4],
        [1, 5],
        [1, 6],
        [1, 7],
        [5, 6],
        [5, 7],
    ]
    assert sheets["cr_scope_impact"]["Impacted_WI_IDs"].tolist() == ["2,3,4,5,6,7", "6,7"]


def test_cycles_start_at_their_smallest_member_shortest_first():
    links = pd.DataFrame(
        [(3, 1, "Blocks"), (1, 2, "Blocks"), (2, 3, "Blocks"), (4, 2, "Blocks"), (2, 4, "Blocks")],
        columns=edges().columns,
    )
    cycles = analyze.GraphAnalyzer(nodes(), links).find_cycles()
    members = cycles.groupby("Cycle_Index")["Node_ID"].apply(list).tolist()
    assert members == [[2, 4], [1, 2, 3]]


def test_equally_long_chains_pick_the_one_with_the_smallest_items():
    links = pd.DataFrame([(3, 4, "Blocks"), (1, 2, "Blocks")], columns=edges().columns)
    for order in (links, links.iloc[::-1]):
        chain = analyze.GraphAnalyzer(nodes(), order).find_longest_chain()
        assert chain["WI_ID"].tolist() == [1, 2]


def test_report_opens_with_about_config_and_summary_and_ends_with_unpacked(tmp_path):
    graph_nodes, links = tangled_graph()
    output = analyze_files(
        tmp_path,
        graph_nodes,
        links,
        "front",
        "--steps",
        "cr_impact_unpacked,cycles,scc@dependency",
    )
    workbook = load_workbook(output)
    try:
        assert workbook.sheetnames == [
            "About",
            "Config",
            "Summary",
            "cycles",
            "scc@dependency",
            "cr_impact_unpacked",
        ]
        about = dict(workbook["About"].iter_rows(min_row=2, values_only=True))
        assert about["Work items loaded"] == 8
        assert about["Links loaded"] == 10
        assert about["Out-of-scope bridge items (contracted)"] == 1
        assert about["Work items analyzed"] == 7
        assert about["Complete"] == "yes"
        assert "hops" in about["Purpose"]
        config = dict(workbook["Config"].iter_rows(min_row=2, values_only=True))
        assert config["steps"] == "cr_impact_unpacked,cycles,scc@dependency"
        assert config["sheets"] == "cycles, scc@dependency, cr_impact_unpacked"
        assert config["max_cycles"] == analyze.DEFAULT_MAX_CYCLES

        summary = workbook["Summary"]
        rows = list(summary.iter_rows(min_row=2, values_only=True))
        assert [row[:7] for row in rows] == [
            ("cycles", "cycles", "all", 5, 2, "cycles", True),
            ("scc@dependency", "scc", "dependency", 7, 5, "components", True),
            ("cr_impact_unpacked", "cr_impact_unpacked", "all", 8, 2, "change requests", True),
        ]
        assert rows[1][7] == "1 with more than one member"
        link = summary["A3"].hyperlink
        assert link.location == "'scc@dependency'!A1"

        cycles = workbook["cycles"]
        assert cycles.auto_filter.ref == cycles.dimensions
        assert cycles.freeze_panes == "A2"
        url_column = [c.value for c in cycles[1]].index("WI_URL") + 1
        url_cell = cycles.cell(row=2, column=url_column)
        assert url_cell.value == "https://ado.example/Coll/_workitems/edit/2"
        assert url_cell.hyperlink.target == url_cell.value
    finally:
        workbook.close()


def test_summary_marks_budgeted_steps_incomplete(tmp_path):
    graph_nodes, links = tangled_graph()
    analyzer = analyze.GraphAnalyzer(
        graph_nodes, links, effort_field="Effort", max_cycles=1, centrality_sources=2
    )
    results = {"cycles": analyzer.find_cycles(), "metrics": analyzer.compute_metrics()}
    steps = [("cycles", "cycles", None), ("metrics", "metrics", None)]
    summary = analyze.summarize_results(results, steps, {})
    assert summary["Complete"].tolist() == [False, False]
    assert "cycle budget" in summary.loc[0, "Note"]
    assert summary.loc[1, "Note"] == "betweenness sampled from 2 sources"
    args = SimpleNamespace(nodes_file="n.parquet", edges_file="e.parquet")
    about = analyze.about_frame(args, analyzer, summary).set_index("Item")["Value"]
    assert about["Complete"] == "no: cycles, metrics"


def test_summary_names_the_spill_file(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "EXCEL_MAX_ROWS", 2)
    output = tmp_path / "report.xlsx"
    results = {"orphans": nodes()}
    spills = report.plan_analysis_report(results, output)
    summary = analyze.summarize_results(results, [("orphans", "orphans", None)], spills)
    assert summary.loc[0, "Note"].startswith("rows written to report.orphans.csv")


def test_url_links_stop_at_excels_per_sheet_limit(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(report, "EXCEL_MAX_HYPERLINKS", 2)
    urls = [f"https://ado.example/_workitems/edit/{i}" for i in range(3)]
    output = tmp_path / "report.xlsx"
    report.write_analysis_report(
        {"many": pd.DataFrame({"WI_URL": urls}), "few": pd.DataFrame({"WI_URL": urls[:2]})},
        output,
    )
    workbook = load_workbook(output)
    try:
        assert workbook["many"]["A2"].hyperlink is None
        assert workbook["few"]["A2"].hyperlink.target == urls[0]
    finally:
        workbook.close()
    assert "Sheet 'many': 3 work-item URLs exceed Excel's 2 hyperlinks" in caplog.text


def test_preamble_sheets_never_take_a_result_sheets_name(tmp_path):
    with pytest.raises(ValueError, match="share a name"):
        report.write_analysis_report(
            {"summary": pd.DataFrame({"a": [1]})},
            tmp_path / "report.xlsx",
            preamble={"Summary": pd.DataFrame({"b": [1]})},
        )
    assert not (tmp_path / "report.xlsx").exists()
