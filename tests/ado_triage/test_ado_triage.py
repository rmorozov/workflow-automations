"""Behavior contracts for ado-triage using synthetic snapshots; no ADO access."""

from datetime import datetime, timedelta

import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook

from workflow_automations import ado_triage as tool

AS_OF = datetime(2026, 10, 1)
BASE = "https://ado.example.com/tfs/Collection/Project"
TEAM_X = "Proj\\TeamX"
TEAM_Y = "Proj\\TeamY"


def rel(kind, target):
    return {"rel": kind, "url": f"{BASE}/_apis/wit/workItems/{target}"}


def item(wi_id, area, title, *, oe=0, cw=0, rw=0, owner="Owner", changed_days=2, **extra):
    fields = {
        tool.FIELD_TITLE: title,
        tool.FIELD_AREA_PATH: area,
        tool.FIELD_STATE: "Active",
        tool.FIELD_ORIGINAL_ESTIMATE: oe,
        tool.FIELD_COMPLETED_WORK: cw,
        tool.FIELD_REMAINING_WORK: rw,
        tool.FIELD_CHANGED_DATE: (AS_OF - timedelta(days=changed_days)).isoformat() + "Z",
    }
    if owner:
        fields[tool.FIELD_ASSIGNED_TO] = {"displayName": owner}
    fields.update(extra)
    return {"id": wi_id, "fields": fields, "relations": []}


def iso(days):
    return (AS_OF + timedelta(days=days)).isoformat() + "Z"


@pytest.fixture
def snapshot():
    items = [
        item(1, TEAM_X, "Big risky CR", oe=50, cw=10, rw=120, **{tool.FIELD_UAT_EXPECTED: iso(20)}),
        item(2, TEAM_X, "Waiting child", rw=30, **{tool.FIELD_PARENT: 1}),
        item(3, TEAM_X, "Almost done", oe=20, cw=15, rw=5),
        item(4, TEAM_Y, "Dormant and unowned", rw=10, owner=None, changed_days=40),
        item(5, TEAM_Y, "Estimate overdue", rw=8, **{tool.FIELD_EST_EXPECTED: iso(-30)}),
        item(6, TEAM_Y, "Far future", rw=15, **{tool.FIELD_UAT_EXPECTED: iso(120)}),
    ]
    # 1 blocks 2 (Successor); 1 depends on 3 (Predecessor); 1 parents 2 (Child).
    items[0]["relations"] = [rel(tool.REL_SUCCESSOR, 2), rel(tool.REL_PREDECESSOR, 3)]
    items[0]["relations"].append(rel(tool.REL_CHILD, 2))
    return tool.Snapshot(items=items, parent_titles={1: "Big risky CR"})


CAPACITY = {TEAM_X: 100.0, TEAM_Y: 500.0}


def settings(**overrides):
    values = {"as_of": AS_OF, "base_url": BASE, "capacity_months": 2}
    values.update(overrides)
    return tool.Settings(**values)


def by_id(report):
    return report.master.set_index(tool.FIELD_ID)


def test_risk_and_opportunity_flags_are_separated(snapshot):
    rows = by_id(tool.build_report(snapshot, CAPACITY, settings()))
    assert set(rows.loc[1, tool.COL_FLAGS].split("|")) == {
        "DATE_DRIFT",
        "BLOCKS_OTHERS",
        "EXCEEDS_MONTH_CAP",
    }
    assert rows.loc[1, tool.COL_SCORE] == 9
    assert set(rows.loc[4, tool.COL_FLAGS].split("|")) == {"STALE", "UNASSIGNED"}
    assert rows.loc[5, tool.COL_FLAGS] == "EST_DEBT_DATE"
    # Opportunities never contribute to attention.
    assert rows.loc[3, tool.COL_OPP_FLAGS] == "QUICK_WIN"
    assert rows.loc[6, tool.COL_OPP_FLAGS] == "DEFER_CANDIDATE"
    assert rows.loc[3, tool.COL_SCORE] == rows.loc[6, tool.COL_SCORE] == 0


def test_blast_radius_counts_only_items_this_item_obstructs(snapshot):
    rows = by_id(tool.build_report(snapshot, CAPACITY, settings()))
    # The Predecessor link (1 depends on 3) must not count as 1 blocking anything.
    assert rows.loc[1, tool.COL_BLOCKS_COUNT] == 1
    assert rows.loc[3, tool.COL_BLOCKS_COUNT] == 0


def test_root_follows_parent_field_and_child_links_are_kept(snapshot):
    report = tool.build_report(snapshot, CAPACITY, settings())
    rows = by_id(report)
    assert rows.loc[1, tool.COL_IS_ROOT] and not rows.loc[2, tool.COL_IS_ROOT]
    assert rows.loc[2, tool.COL_PARENT_TITLE] == "Big risky CR"
    assert "Child" in set(report.relations[tool.REL_COL_TYPE])


def test_unknown_linked_items_are_labelled_not_dropped():
    snap = tool.Snapshot(items=[item(1, TEAM_X, "A")])
    snap.items[0]["relations"] = [
        rel(tool.REL_BLOCKS_FORWARD, 99),
        rel(tool.REL_BLOCKS_FORWARD, 98),
    ]
    snap.external = {98: {tool.FIELD_TITLE: "Outside", tool.FIELD_AREA_PATH: TEAM_Y}}
    relations = tool.build_report(snap, {}, settings()).relations.set_index(tool.REL_COL_DEST_ID)
    assert relations.loc[99, tool.REL_COL_DEST_TITLE] == "UNKNOWN"
    assert relations.loc[98, tool.REL_COL_DEST_AREA] == TEAM_Y


def test_hierarchical_capacity_load_and_rag(snapshot):
    capacity = {"Proj": 2000.0, TEAM_X: 100.0}
    report = tool.build_report(snapshot, capacity, settings())
    summary = report.sheets[tool.SHEET_SUMMARY].set_index("Team")
    assert summary.loc[TEAM_X, tool.COL_RAG] == "RED"
    assert summary.loc[TEAM_X, tool.COL_LOAD_PCT] == 155.0
    # TeamY has no row of its own and resolves to the parent level.
    assert summary.loc["Proj", tool.COL_RAG] == "GREEN"
    assert list(summary.index)[0] == TEAM_X  # RED sorts first


def test_undefined_capacity_is_grey_not_green(snapshot):
    summary = tool.build_report(snapshot, {}, settings()).sheets[tool.SHEET_SUMMARY]
    assert set(summary[tool.COL_RAG]) == {"GREY"}
    assert set(summary[tool.COL_CAPACITY]) == {tool.UNDEFINED_CAPACITY}


def test_top_20_percent_keeps_at_least_one_per_area():
    items = [item(i, TEAM_X, f"x{i}", rw=i) for i in range(1, 11)] + [item(20, TEAM_Y, "y", rw=3)]
    sheet = tool.build_report(tool.Snapshot(items=items), {}, settings()).sheets[
        "15. Top 20% Remaining Work"
    ]
    assert sorted(sheet[tool.FIELD_ID]) == [9, 10, 20]


def test_deltas_persistence_new_and_resolved(snapshot, tmp_path):
    first = tool.build_report(snapshot, CAPACITY, settings())
    path = tmp_path / "week1.xlsx"
    tool.write_excel(path, first, {"As of": "2026-10-01"})

    # Next week: item 4 gets an owner and activity; a new oversized item appears.
    snapshot.items[3] = item(4, TEAM_Y, "Dormant and unowned", rw=10)
    snapshot.items.append(item(7, TEAM_Y, "Brand new", rw=600))
    previous = tool.read_previous_master(path)
    second = tool.build_report(snapshot, CAPACITY, settings(), previous)
    rows = by_id(second)

    assert rows.loc[1, tool.COL_DELTA] == "CARRIED" and rows.loc[1, tool.COL_PERSIST] == 2
    assert rows.loc[7, tool.COL_DELTA] == "NEW" and rows.loc[7, tool.COL_PERSIST] == 1
    assert rows.loc[4, tool.COL_PERSIST] == 0
    resolved = second.sheets[tool.SHEET_RESOLVED].set_index(tool.FIELD_ID)
    assert list(resolved.index) == [4]
    assert resolved.loc[4, "Resolution"] == "Flags cleared"


def test_legacy_previous_report_without_flags_degrades(snapshot, tmp_path):
    path = tmp_path / "legacy.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({tool.FIELD_ID: [1, 2]}).to_excel(
            writer, sheet_name="1. Master Data", index=False
        )
    report = tool.build_report(snapshot, CAPACITY, settings(), tool.read_previous_master(path))
    assert by_id(report).loc[1, tool.COL_DELTA] == "CARRIED"
    assert by_id(report).loc[5, tool.COL_DELTA] == "NEW"
    assert any("predates flags" in note for note in report.notes)


def test_workbook_layout_omissions_and_profiles(snapshot, tmp_path):
    report = tool.build_report(snapshot, CAPACITY, settings())
    path = tmp_path / "full.xlsx"
    tool.write_excel(path, report, {"As of": "2026-10-01"})
    wb = load_workbook(path)
    assert wb.sheetnames[0] == tool.SHEET_CONFIG
    assert all(len(name) <= 31 for name in wb.sheetnames)
    assert tool.SHEET_RESOLVED not in wb.sheetnames  # empty without a previous report
    assert tool.SHEET_RESOLVED in report.omitted
    agenda = wb[tool.SHEET_AGENDA]
    assert agenda["A1"].value.startswith("Top items") and agenda.freeze_panes == "A4"
    first_agenda = pd.read_excel(path, sheet_name=tool.SHEET_AGENDA, header=2)
    assert first_agenda.iloc[0][tool.FIELD_ID] == 1

    exec_report = tool.build_report(snapshot, CAPACITY, settings(profile="exec"))
    assert tool.SHEET_MASTER not in exec_report.sheets
    assert tool.SHEET_SUMMARY in exec_report.sheets


def capacity_workbook(path, headers, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = tool.CAPACITY_SHEET_NAME
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_capacity_accepts_excel_date_headers_and_sums_window(tmp_path):
    path = capacity_workbook(
        tmp_path / "cap.xlsx",
        ["AreaPath", datetime(2026, 11, 1), "2026-12", "2027-01"],
        [[TEAM_X, 40, 60, 999], [" Proj ", 10, None, 0]],
    )
    months = tool.target_months(None, 2, AS_OF)
    assert months == ("2026-11", "2026-12")
    assert tool.load_capacity(path, months) == {TEAM_X: 100.0, "Proj": 10.0}


@pytest.mark.parametrize(
    ("headers", "rows", "message"),
    [
        (["AreaPath", "2026-11"], [[TEAM_X, 1]], "not found"),
        (["AreaPath", "2026-11", "2026-12"], [[TEAM_X, 1, 1], [TEAM_X, 2, 2]], "duplicate"),
    ],
)
def test_capacity_rejects_partial_window_and_duplicates(tmp_path, headers, rows, message):
    path = capacity_workbook(tmp_path / "cap.xlsx", headers, rows)
    with pytest.raises(tool.TriageError, match=message):
        tool.load_capacity(path, ("2026-11", "2026-12"))


def test_target_months_rolls_over_year_and_validates():
    assert tool.target_months("2026-12", 2, AS_OF) == ("2026-12", "2027-01")
    with pytest.raises(tool.TriageError):
        tool.target_months("12/2026", 2, AS_OF)


def cli(tmp_path, *extra):
    return [
        "export",
        "--url",
        BASE,
        "--query-id",
        "00000000-0000-0000-0000-000000000000",
        "--as-of",
        "2026-10-01",
        *extra,
    ]


def test_cli_end_to_end_with_network_layer_replaced(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "fetch_snapshot", lambda url, query: snapshot)
    out = tmp_path / "out" / "report.xlsx"
    assert tool.main(cli(tmp_path, "--output", str(out))) == tool.EXIT_OK
    assert tool.SHEET_SUMMARY in load_workbook(out).sheetnames
    assert not [p for p in out.parent.iterdir() if p.name.startswith(".")]  # temp cleaned

    # Existing output is preserved without --overwrite.
    before = out.read_bytes()
    assert tool.main(cli(tmp_path, "--output", str(out))) == tool.EXIT_VALIDATION
    assert out.read_bytes() == before
    second = cli(tmp_path, "--output", str(out), "--overwrite", "--previous-file", str(out))
    assert tool.main(second) == tool.EXIT_VALIDATION  # output may not replace its own input

    nxt = tmp_path / "next.xlsx"
    assert tool.main(cli(tmp_path, "--output", str(nxt), "--previous-file", str(out))) == 0


def test_cli_validates_local_inputs_before_network(tmp_path, monkeypatch):
    def unreachable(url, query):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(tool, "fetch_snapshot", unreachable)
    cap = capacity_workbook(tmp_path / "cap.xlsx", ["AreaPath", "2026-11"], [[TEAM_X, 1]])
    args = cli(tmp_path, "--output", str(tmp_path / "r.xlsx"), "--capacity-file", str(cap))
    assert tool.main(args) == tool.EXIT_VALIDATION


@pytest.mark.parametrize(
    ("error", "code"),
    [(tool.AdoError("down"), tool.EXIT_ADO), (tool.EmptyQueryError("none"), tool.EXIT_EMPTY)],
)
def test_cli_exit_codes_for_ado_failures(tmp_path, monkeypatch, error, code):
    def failing(url, query):
        raise error

    monkeypatch.setattr(tool, "fetch_snapshot", failing)
    out = tmp_path / "r.xlsx"
    assert tool.main(cli(tmp_path, "--output", str(out))) == code
    assert not out.exists()


def test_csv_output_is_master_data(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "fetch_snapshot", lambda url, query: snapshot)
    out = tmp_path / "r.csv"
    assert tool.main(cli(tmp_path, "--output", str(out), "--format", "csv")) == 0
    frame = pd.read_csv(out)
    assert {tool.COL_FLAGS, tool.COL_SCORE} <= set(frame.columns)
    assert len(frame) == len(snapshot.items)
