"""Behavior of team-capacity-calibrate: observed bug share and logged-to-predicted hours."""

import datetime as dt
import json

import pandas as pd
import pytest

from workflow_automations import team_capacity as tc
from workflow_automations import team_capacity_calibrate as cal
from workflow_automations.ado_common.snapshot import RawSnapshot

D = dt.date
BASE = 80  # one developer, 10 days x 8 h, no rituals or leave
NET = BASE * (1 - 0.5 - 0.12)


def config_book():
    return {
        "settings": pd.DataFrame(
            {"key": ["default_country", "sick_days", "vacation_days"], "value": ["X", 0, 0]}
        ),
        "countries": pd.DataFrame({"country": ["X"]}),
        "calendar": pd.DataFrame({"country": ["X"], "date": [D(2026, 12, 25)], "hours": [0]}),
        "role_groups": pd.DataFrame(
            {
                "title_class": ["developer", "testing engineer"],
                "group": ["developer", "test"],
                "bug_fix": ["yes", "no"],
                "triage_pool": ["yes", "no"],
            }
        ),
        "iterations": pd.DataFrame(
            {
                "name": ["S1", "S2", "S3"],
                "start": [D(2026, 1, 5), D(2026, 1, 19), D(2026, 2, 2)],
                "end": [D(2026, 1, 16), D(2026, 1, 30), D(2026, 2, 13)],
                "phase": ["development", "development", "stabilization"],
            }
        ),
    }


ROSTER = pd.DataFrame(
    {
        "email": ["a@x", "b@x", "t@x"],
        "classified_title": ["developer", "developer", "testing engineer"],
        "area_path": ["Proj\\A", "Proj\\B", "Proj\\B"],
    }
)


def item(wi_id, kind, area, sprint, who, hours, parent=None):
    relations = []
    if parent:
        relations.append(
            {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"https://ado.example/_apis/wit/workItems/{parent}",
            }
        )
    return {
        "id": wi_id,
        "fields": {
            "System.WorkItemType": kind,
            "System.AreaPath": area,
            "System.IterationPath": f"Proj\\{sprint}",
            "System.AssignedTo": {"uniqueName": who, "displayName": who.split("@")[0]}
            if "@" in who
            else who,
            "Microsoft.VSTS.Scheduling.CompletedWork": hours,
        },
        "relations": relations,
    }


ITEMS = [
    item(1, "Bug", "Proj\\A", "S1", "a@x", 10),
    item(2, "Task", "Proj\\A\\Sub", "S1", "A@X", 6, parent=1),  # a task under a bug
    item(3, "Task", "Proj\\A", "S1", "Ann <a@x>", 24),
    item(4, "Task", "Proj\\A", "S2", "a@x", 30),
    item(5, "Bug", "Proj\\A", "S2", "a@x", 10),
    item(6, "Bug", "Proj\\A", "S3", "a@x", 30),
    item(7, "Task", "Proj\\A", "S3", "a@x", 10),
    item(8, "Bug", "Proj\\B", "S1", "b@x", 20),
    item(9, "Task", "Proj\\B", "S1", "b@x", 20),
    item(10, "Task", "Proj\\B", "S1", "t@x", 15),  # a tester: not the triage pool
    item(11, "Task", "Proj\\B", "S2", "b@x", 40),
    item(12, "Task", "Other\\X", "S2", "b@x", 5),
    item(13, "Task", "Proj\\A", "Sprint 99", "a@x", 7),
    item(14, "Task", "Proj\\A", "S2", "a@x", None),
]


def snapshot():
    return RawSnapshot(
        producer="ado-triage", source={}, roots=[i["id"] for i in ITEMS], items=ITEMS
    )


def outputs():
    config = tc.read_config(config_book(), tc.roster_emails(ROSTER))
    return cal.calibrate(snapshot(), ROSTER, config, {"bug"})


def test_bug_share_and_accuracy_per_area_and_iteration():
    result = outputs()["calibration"].set_index(["area_path", "iteration"])
    expected = {
        ("Proj\\A", "S1"): (16, 24, 0.4),
        ("Proj\\A", "S2"): (10, 30, 0.25),
        ("Proj\\A", "S3"): (30, 10, 0.75),
        ("Proj\\B", "S1"): (20, 20, 0.5),
        ("Proj\\B", "S2"): (0, 40, 0.0),
    }
    for key, (bug, other, share) in expected.items():
        row = result.loc[key]
        assert (row["bug_hours"], row["other_hours"]) == (bug, other)
        assert row["observed_bug_share"] == pytest.approx(share)
        assert row["suggested_bug_fix_quota"] == pytest.approx(share * 0.88)
        assert row["logged_to_predicted"] == pytest.approx(other / NET)
        assert row["predicted_base"] == pytest.approx(BASE)
        assert row["configured_bug_fix_quota"] == pytest.approx(0.5)


def test_iteration_without_logged_work_is_reported_not_zero():
    out = outputs()
    row = out["calibration"].set_index(["area_path", "iteration"]).loc[("Proj\\B", "S3")]
    assert row["status"] == "no logged work"
    assert pd.isna(row["observed_bug_share"]) and pd.isna(row["logged_to_predicted"])
    problems = out["problems"]
    assert ["Proj\\B", "no logged work", "iteration S3"] in problems[
        ["area_path", "problem", "detail"]
    ].values.tolist()


def test_work_outside_the_model_is_counted_in_problems():
    details = outputs()["problems"]["detail"].tolist()
    assert "5 hours: AreaPath outside the roster's triage pool" in details
    assert "7 hours: iteration not in the config" in details
    assert "15 hours: assigned outside the triage pool or unassigned" in details


def test_suggested_rows_follow_phases_and_load_as_a_rates_sheet():
    suggested = outputs()["suggested_rates"]
    rows = suggested[["area_path", "from", "to", "phase", "bug_fix_quota"]].values.tolist()
    assert rows == [
        ["Proj\\A", D(2026, 1, 5), D(2026, 1, 30), "development", round(26 * 0.88 / 80, 3)],
        ["Proj\\A", D(2026, 2, 2), D(2026, 2, 13), "stabilization", round(30 * 0.88 / 40, 3)],
        ["Proj\\B", D(2026, 1, 5), D(2026, 1, 30), "development", round(20 * 0.88 / 80, 3)],
    ]
    book = config_book()
    book["rates"] = suggested
    config = tc.read_config(book, tc.roster_emails(ROSTER))
    assert config.rate("Proj\\A\\Sub", D(2026, 2, 3), "bug_fix_quota") == pytest.approx(0.66)
    assert config.rate("Proj\\A", D(2026, 2, 3), "focus_loss") == pytest.approx(0.12)


def test_cli_writes_the_workbook(tmp_path):
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps(snapshot().to_json()), encoding="utf-8")
    roster = tmp_path / "roster.xlsx"
    ROSTER.to_excel(roster, index=False)
    config = tmp_path / "config.xlsx"
    tc.write_book({k: tc.excel_dates(v) for k, v in config_book().items()}, config)
    output = tmp_path / "calibration.xlsx"
    args = [
        "--snapshot",
        str(snap),
        "--roster",
        str(roster),
        "--config",
        str(config),
        "--output",
        str(output),
    ]
    assert cal.main(args) == 0
    assert list(pd.read_excel(output, sheet_name=None)) == [
        "calibration",
        "suggested_rates",
        "problems",
    ]
    assert cal.main(args) == 1  # exists
    assert cal.main([*args, "--overwrite"]) == 0
    # The config flags of team-capacity apply here too.
    calendar = tmp_path / "cal.csv"
    calendar.write_text("country,date,hours\nX,2026-01-05,0\n", encoding="utf-8")
    extra = ["--overwrite", "--set", "focus_loss=0", "--calendar", str(calendar)]
    assert cal.main([*args, *extra]) == 0
    rows = pd.read_excel(output, sheet_name="calibration")
    s1 = rows[(rows["area_path"] == "Proj\\A") & (rows["iteration"] == "S1")].iloc[0]
    assert s1["predicted_base"] == pytest.approx(72)
    assert s1["suggested_bug_fix_quota"] == pytest.approx(0.4)


def test_config_without_iterations_is_refused():
    book = config_book()
    del book["iterations"]
    book["settings"] = pd.concat(
        [book["settings"], pd.DataFrame({"key": ["iteration_start"], "value": [D(2026, 1, 5)]})]
    )
    config = tc.read_config(book, tc.roster_emails(ROSTER))
    with pytest.raises(tc.CapacityError, match="needs an iterations sheet"):
        cal.calibrate(snapshot(), ROSTER, config, {"bug"})


@pytest.mark.parametrize(
    ("value", "email"),
    [
        ({"uniqueName": "A@x"}, "a@x"),
        ("Ann Lee <ann@x.example>", "ann@x.example"),
        ("ann@x.example", "ann@x.example"),
        ("Ann Lee", ""),
        (None, ""),
    ],
)
def test_assignee_email(value, email):
    assert cal.assignee_email(value) == email


def test_configured_iteration_without_any_item_is_reported():
    items = [i for i in ITEMS if i["fields"]["System.IterationPath"] != "Proj\\S2"]
    snap = RawSnapshot(
        producer="ado-triage", source={}, roots=[i["id"] for i in items], items=items
    )
    config = tc.read_config(config_book(), tc.roster_emails(ROSTER))
    result = cal.calibrate(snap, ROSTER, config, {"bug"})["calibration"]
    s2 = result[result["iteration"] == "S2"]
    assert s2["status"].tolist() == ["no logged work", "no logged work"]


def test_an_unusable_iteration_ends_a_suggested_run():
    book = config_book()
    book["iterations"]["phase"] = ["development", "stabilization", "development"]
    items = [
        item(1, "Bug", "Proj\\A", "S1", "a@x", 10),
        item(2, "Task", "Proj\\A", "S3", "a@x", 10),
    ]
    snap = RawSnapshot(producer="ado-triage", source={}, roots=[1, 2], items=items)
    config = tc.read_config(book, tc.roster_emails(ROSTER))
    suggested = cal.calibrate(snap, ROSTER, config, {"bug"})["suggested_rates"]
    rows = suggested[suggested["area_path"] == "Proj\\A"][["from", "to", "bug_fix_quota"]]
    assert rows.values.tolist() == [
        [D(2026, 1, 5), D(2026, 1, 16), 0.88],
        [D(2026, 2, 2), D(2026, 2, 13), 0.0],
    ]


def test_focus_loss_changing_inside_an_iteration_is_weighted_by_day():
    book = config_book()
    book["rates"] = pd.DataFrame(
        {
            "area_path": ["Proj"],
            "from": [D(2026, 1, 12)],
            "to": [D(2026, 1, 16)],
            "focus_loss": [0.5],
            "bug_fix_quota": [0.4],
        }
    )
    items = [
        item(1, "Bug", "Proj\\A", "S1", "a@x", 20),
        item(2, "Task", "Proj\\A", "S1", "a@x", 20),
    ]
    snap = RawSnapshot(producer="ado-triage", source={}, roots=[1, 2], items=items)
    config = tc.read_config(book, tc.roster_emails(ROSTER))
    result = cal.calibrate(snap, ROSTER, config, {"bug"})
    row = result["calibration"].set_index(["area_path", "iteration"]).loc[("Proj\\A", "S1")]
    focus = (40 * 0.12 + 40 * 0.5) / 80  # 0.31, as the model deducts it
    assert row["suggested_bug_fix_quota"] == pytest.approx(0.5 * (1 - focus))
    suggested = result["suggested_rates"]
    rows = suggested[suggested["area_path"] == "Proj\\A"][["from", "to", "bug_fix_quota"]]
    assert rows.values.tolist() == [
        [D(2026, 1, 5), D(2026, 1, 11), 0.44],
        [D(2026, 1, 12), D(2026, 1, 16), 0.25],
    ]


def test_suggested_rates_fit_the_focus_loss_they_inherit():
    # All logged work is bug work while focus loss changes inside the iteration:
    # one averaged quota (0.69) plus the inherited 0.5 would be more than 1.
    book = config_book()
    book["rates"] = pd.DataFrame(
        {
            "area_path": ["Proj"],
            "from": [D(2026, 1, 12)],
            "to": [D(2026, 1, 16)],
            "focus_loss": [0.5],
            "bug_fix_quota": [0.4],
        }
    )
    items = [item(1, "Bug", "Proj\\A", "S1", "a@x", 40)]
    snap = RawSnapshot(producer="ado-triage", source={}, roots=[1], items=items)
    config = tc.read_config(book, tc.roster_emails(ROSTER))
    suggested = cal.calibrate(snap, ROSTER, config, {"bug"})["suggested_rates"]
    rows = suggested[suggested["area_path"] == "Proj\\A"][["from", "to", "bug_fix_quota"]]
    assert rows.values.tolist() == [
        [D(2026, 1, 5), D(2026, 1, 11), 0.88],
        [D(2026, 1, 12), D(2026, 1, 16), 0.5],
    ]
    book["rates"] = pd.concat([book["rates"], suggested], ignore_index=True)
    applied = tc.read_config(book, tc.roster_emails(ROSTER))
    days = tc.compute(ROSTER, applied, D(2026, 1, 5), D(2026, 1, 16)).days
    a = days[(days["area_path"] == "Proj\\A") & (days["gross"] > 0)]
    assert len(a) == 10
    assert a["net"].sum() == pytest.approx(0)
