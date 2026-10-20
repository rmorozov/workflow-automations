"""Behavior of team-capacity: the working-day calendar, the taxes and the output workbook."""

import datetime as dt

import pandas as pd
import pytest

from workflow_automations import team_capacity as tc

D = dt.date


def config_book(**sheets):
    """A minimal valid config: one country (X, Mon-Fri 8 h), no rituals, no leave budgets.

    The cadence starts on 2025-12-29, so 2026-11-02 also starts an iteration.
    """
    book = {
        "settings": pd.DataFrame(
            {
                "key": ["iteration_start", "default_country", "sick_days", "vacation_days"],
                "value": [D(2025, 12, 29), "X", 0, 0],
            }
        ),
        "countries": pd.DataFrame({"country": ["X"], "workdays": ["Mon-Fri"]}),
        "calendar": pd.DataFrame({"country": ["X"], "date": [D(2026, 12, 25)], "hours": [0]}),
        "role_groups": pd.DataFrame(
            {
                "title_class": ["developer", "team lead", "testing engineer"],
                "group": ["developer", "team lead", "test"],
                "allocation": [1.0, 0.5, 1.0],
                "bug_fix": ["yes", "yes", "no"],
                "triage_pool": ["yes", "yes", "no"],
            }
        ),
    }
    for name, value in sheets.items():
        if isinstance(value, dict) and name == "settings":
            extra = pd.DataFrame({"key": list(value), "value": list(value.values())})
            base = book["settings"][~book["settings"]["key"].isin(value)]
            book["settings"] = pd.concat([base, extra], ignore_index=True)
        else:
            book[name] = pd.DataFrame(value)
    return book


def roster(*rows):
    defaults = {"classified_title": "developer", "area_path": "P\\A", "approved": True}
    return pd.DataFrame([{**defaults, **row} for row in rows])


def run(people, first, last, **sheets):
    frame = roster(*people)
    config = tc.read_config(config_book(**sheets), tc.roster_emails(frame))
    return tc.compute(frame, config, first, last), config


def by_date(result):
    return result.days.set_index("date")


NO_TAX = {"settings": {"bug_fix_quota": 0, "focus_loss": 0}}

# ---------------------------------------------------------------------------
# Calendar (CP-001)
# ---------------------------------------------------------------------------


def test_holidays_and_absences_remove_exactly_their_days():
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 12, 21),
        D(2026, 12, 31),
        absences={
            "email": ["a@x"],
            "from": [D(2026, 12, 29)],
            "to": [D(2026, 12, 30)],
            "type": ["other"],
        },
        **NO_TAX,
    )
    days = by_date(result)
    assert days.loc[D(2026, 12, 25), "gross"] == 0  # holiday
    assert days.loc[D(2026, 12, 29), "absence"] == 8
    assert days.loc[D(2026, 12, 30), "net"] == 0
    assert days.loc[D(2026, 12, 31), "net"] == 8
    assert days["net"].sum() == 8 * 6  # 9 weekdays - 1 holiday - 2 absent


def test_short_day_keeps_the_day_and_working_saturday_adds_one():
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 11, 2),
        D(2026, 11, 8),
        calendar={"country": ["X", "X"], "date": [D(2026, 11, 3), D(2026, 11, 7)], "hours": [7, 8]},
        rituals={"name": ["daily"], "per": ["day"], "hours": [0.5], "groups": ["all"]},
        **NO_TAX,
    )
    days = by_date(result)
    assert days.loc[D(2026, 11, 3), "gross"] == 7
    assert days.loc[D(2026, 11, 3), "ritual"] == 0.5  # still has its daily scrum
    assert days.loc[D(2026, 11, 7), "gross"] == 8  # working Saturday
    assert days.loc[D(2026, 11, 8), "gross"] == 0
    assert days["gross"].sum() == 5 * 8 - 1 + 8


def test_part_day_absence_removes_only_its_hours():
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 11, 2),
        D(2026, 11, 2),
        absences={
            "email": ["a@x"],
            "from": [D(2026, 11, 2)],
            "to": [D(2026, 11, 2)],
            "type": ["other"],
            "hours": [3],
        },
        **NO_TAX,
    )
    assert by_date(result).loc[D(2026, 11, 2), ["absence", "net"]].tolist() == [3, 5]


def test_new_year_week_puts_all_its_hours_in_december():
    result, _ = run(
        [{"email": "a@x"}],
        D(2025, 12, 29),
        D(2026, 1, 4),
        settings={"iteration_start": D(2025, 12, 29), "bug_fix_quota": 0, "focus_loss": 0},
        calendar={"country": ["X", "X"], "date": [D(2026, 1, 1), D(2026, 1, 2)], "hours": [0, 0]},
    )
    months = result.days.groupby("month")["gross"].sum()
    assert months.to_dict() == {"2025-12": 24, "2026-01": 0}


def test_unbooked_calendar_day_leave_becomes_working_days_spread_over_the_year():
    result, config = run(
        [{"email": "a@x"}],
        D(2026, 1, 1),
        D(2026, 12, 31),
        settings={"vacation_days": 30, "bug_fix_quota": 0, "focus_loss": 0},
    )
    year = result.days
    # Expected leave over the year: 30 calendar days x 5/7.
    assert year["vacation"].sum() / 8 == pytest.approx(30 * 5 / 7)
    assert year["illness"].sum() == 0


def test_booked_sick_days_reduce_only_the_sick_budget():
    absences = {"email": ["a@x"], "from": [D(2026, 3, 2)], "to": [D(2026, 3, 4)], "type": ["sick"]}
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 1, 1),
        D(2026, 12, 31),
        settings={
            "sick_days": 7,
            "vacation_days": 20,
            "vacation_unit": "working",
            "bug_fix_quota": 0,
            "focus_loss": 0,
        },
        absences=absences,
    )
    days = result.days
    assert days["absence"].sum() / 8 == 3
    assert days["illness"].sum() / 8 == pytest.approx(4)  # 7 - 3 booked
    assert days["vacation"].sum() / 8 == pytest.approx(20)


def test_booking_later_in_the_year_lowers_the_spread_without_counting_twice():
    def leave(absences):
        result, _ = run(
            [{"email": "a@x"}],
            D(2026, 1, 1),
            D(2026, 12, 31),
            settings={
                "vacation_days": 22,
                "vacation_unit": "working",
                "bug_fix_quota": 0,
                "focus_loss": 0,
            },
            **({"absences": absences} if absences else {}),
        )
        return result.days

    free = leave(None)
    booked = leave(
        {"email": ["a@x"], "from": [D(2026, 8, 3)], "to": [D(2026, 8, 7)], "type": ["vacation"]}
    )
    assert free["vacation"].sum() / 8 == pytest.approx(22)
    assert booked["absence"].sum() / 8 == 5
    assert booked["vacation"].sum() / 8 == pytest.approx(17)
    assert booked["net"].sum() == pytest.approx(free["net"].sum())


def test_over_booked_leave_is_a_problem_and_counts_zero():
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 1, 1),
        D(2026, 1, 31),
        settings={"vacation_days": 2, "vacation_unit": "working"},
        absences={
            "email": ["a@x"],
            "from": [D(2026, 1, 5)],
            "to": [D(2026, 1, 9)],
            "type": ["vacation"],
        },
    )
    assert result.days["vacation"].sum() == 0
    assert [p.problem for p in result.problems] == ["vacation over-booked"]


def test_absences_from_counts_earlier_leave_as_taken():
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 10, 1),
        D(2026, 12, 31),
        settings={
            "vacation_days": 22,
            "vacation_unit": "working",
            "bug_fix_quota": 0,
            "focus_loss": 0,
            "absences_from": D(2026, 10, 1),
        },
    )
    assert result.days["vacation"].sum() / 8 == pytest.approx(22 * 92 / 365)


def test_joiners_and_leavers_count_only_inside_their_dates():
    result, _ = run(
        [
            {"email": "j@x", "start_date": "2026-11-04", "ramp_up_months": 0},
            {"email": "l@x", "end_date": "2026-11-03"},
        ],
        D(2026, 11, 2),
        D(2026, 11, 6),
        **NO_TAX,
    )
    totals = result.days.groupby("email")["gross"].sum()
    assert totals.to_dict() == {"j@x": 24, "l@x": 16}


def test_part_time_hours_scale_short_days():
    result, _ = run(
        [{"email": "a@x", "hours_per_day": 4}],
        D(2026, 11, 2),
        D(2026, 11, 3),
        calendar={"country": ["X"], "date": [D(2026, 11, 3)], "hours": [6]},
        **NO_TAX,
    )
    assert result.days["gross"].tolist() == [4, 3]


def test_iteration_crossing_a_month_splits_its_days_by_month():
    result, config = run(
        [{"email": "a@x"}],
        D(2026, 1, 1),
        D(2026, 12, 31),
        **NO_TAX,
        rituals={"name": ["retro"], "per": ["iteration"], "hours": [1], "groups": ["developer"]},
    )
    days = result.days
    crossing = days[days["iteration"] == "2026-01-26"]  # Jan 26 - Feb 6
    assert crossing.groupby("month")["gross"].sum().to_dict() == {"2026-01": 40, "2026-02": 40}
    assert crossing["ritual"].sum() == pytest.approx(1)
    assert crossing.groupby("month")["ritual"].sum().to_dict() == {"2026-01": 0.5, "2026-02": 0.5}


def test_iterations_sheet_replaces_the_cadence():
    result, config = run(
        [{"email": "a@x"}],
        D(2026, 11, 2),
        D(2026, 11, 20),
        iterations={
            "name": ["S1", "S2"],
            "start": [D(2026, 11, 2), D(2026, 11, 16)],
            "end": [D(2026, 11, 13), D(2026, 11, 27)],
        },
    )
    working = result.days[result.days["gross"] > 0]
    assert working.groupby("iteration")["date"].count().to_dict() == {"S1": 10, "S2": 5}
    assert config.iteration_of(D(2026, 11, 30)) is None


@pytest.mark.parametrize(
    ("sheets", "message"),
    [
        (
            {
                "absences": {
                    "email": ["nobody@x"],
                    "from": ["2026-01-05"],
                    "to": ["2026-01-05"],
                    "type": ["vacation"],
                }
            },
            "absences row 2: email 'nobody@x' is not in the roster",
        ),
        (
            {
                "absences": {
                    "email": ["a@x"],
                    "from": ["2026-01-05"],
                    "to": ["2026-01-02"],
                    "type": ["vacation"],
                }
            },
            "absences row 2: to 2026-01-02 is before from 2026-01-05",
        ),
        (
            {
                "absences": {
                    "email": ["a@x"],
                    "from": ["5 Jan"],
                    "to": ["2026-01-02"],
                    "type": ["leave"],
                }
            },
            "absences row 2 from: '5 Jan' is not a date",
        ),
        (
            {"calendar": {"country": ["Y"], "date": ["2026-01-01"], "hours": [0]}},
            "calendar row 2: unknown country 'Y'",
        ),
        (
            {
                "iterations": {
                    "name": ["S1", "S2"],
                    "start": ["2026-01-05", "2026-01-12"],
                    "end": ["2026-01-16", "2026-01-23"],
                }
            },
            "iterations: 'S1' and 'S2' overlap",
        ),
        (
            {"iterations": {"name": ["S1"], "start": ["2026-01-06"], "end": ["2026-01-16"]}},
            "iterations row 2: start 2026-01-06 is not a Monday",
        ),
        (
            {"settings": {"iteration_start": D(2026, 1, 6)}},
            "settings: iteration_start 2026-01-06 is not a Monday",
        ),
        (
            {
                "rates": {
                    "area_path": ["P", "p"],
                    "from": ["2026-01-01", "2026-01-01"],
                    "to": ["2026-03-31", "2026-06-30"],
                    "bug_fix_quota": [0.6, 0.7],
                }
            },
            "rates rows 2 and 3: same AreaPath and start",
        ),
    ],
)
def test_input_errors_name_the_offending_row(sheets, message):
    with pytest.raises(tc.CapacityError, match=message.replace("(", r"\(")):
        run([{"email": "a@x"}], D(2026, 1, 1), D(2026, 1, 31), **sheets)


def test_unknown_country_in_the_roster_fails():
    with pytest.raises(tc.CapacityError, match="roster row 2: unknown country 'Z'"):
        run([{"email": "a@x", "country": "Z"}], D(2026, 1, 1), D(2026, 1, 31))


def test_area_countries_set_the_country_of_a_subtree():
    book = config_book(
        countries={"country": ["X", "Y"], "workdays": ["Mon-Fri", "Sun-Thu"]},
        area_countries={"area_path": ["P\\B"], "country": ["Y"]},
    )
    frame = roster({"email": "a@x"}, {"email": "b@x", "area_path": "P\\B\\Team"})
    config = tc.read_config(book, tc.roster_emails(frame))
    result = tc.compute(frame, config, D(2026, 11, 1), D(2026, 11, 7))  # Sunday to Saturday
    worked = result.days[result.days["gross"] > 0].groupby("email")["date"].min()
    assert worked.to_dict() == {"a@x": D(2026, 11, 2), "b@x": D(2026, 11, 1)}


# ---------------------------------------------------------------------------
# Taxes (CP-002)
# ---------------------------------------------------------------------------

RITUALS = {
    "name": ["daily scrum", "code review", "pre-planning", "demo", "retro", "1 on 1"],
    "per": ["day", "day", "iteration", "iteration", "iteration", "iteration"],
    "hours": [0.5, 1, 1, 1, 1, 0.5],
    "groups": ["all", "developer, team lead", "all", "all", "all", "all"],
}


def test_worked_example_from_the_design():
    parts = [tc.net_hours(8, 1.5, 3.5, 10, 0.04, 0, 0.5, 0.12) for _ in range(10)]
    total = {key: sum(p[key] for p in parts) for key in parts[0]}
    assert total == pytest.approx(
        {
            "ritual": 18.5,
            "illness": 2.46,
            "vacation": 0,
            "bug_fix": 29.52,
            "focus": 7.08,
            "net": 22.44,
        },
        abs=0.01,
    )


def test_one_iteration_end_to_end_with_default_rates():
    result, _ = run([{"email": "a@x"}], D(2026, 11, 2), D(2026, 11, 13), rituals=RITUALS)
    total = result.days[list(tc.COMPONENTS)].sum()
    assert total["gross"] == 80
    assert total["ritual"] == pytest.approx(18.5)
    assert total["bug_fix"] == pytest.approx(61.5 * 0.5)
    assert total["focus"] == pytest.approx(61.5 * 0.12)
    assert total["net"] == pytest.approx(61.5 * 0.38)
    assert total["gross"] - total.drop(["gross", "net"]).sum() == pytest.approx(total["net"])


def test_a_person_away_for_the_whole_iteration_pays_no_rituals():
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 11, 2),
        D(2026, 11, 13),
        rituals=RITUALS,
        absences={
            "email": ["a@x"],
            "from": [D(2026, 11, 2)],
            "to": [D(2026, 11, 13)],
            "type": ["other"],
        },
    )
    assert result.days["ritual"].sum() == 0
    assert result.days["absence"].sum() == 80


def test_iteration_rituals_spread_over_available_days():
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 11, 2),
        D(2026, 11, 13),
        rituals={"name": ["retro"], "per": ["iteration"], "hours": [3.5], "groups": ["all"]},
        absences={
            "email": ["a@x"],
            "from": [D(2026, 11, 2)],
            "to": [D(2026, 11, 6)],
            "type": ["other"],
        },
    )
    assert result.days["ritual"].sum() == pytest.approx(3.5)
    assert by_date(result).loc[D(2026, 11, 9), "ritual"] == pytest.approx(0.7)


def test_allocation_scales_every_component():
    full, _ = run([{"email": "a@x"}], D(2026, 11, 2), D(2026, 11, 13), rituals=RITUALS)
    half, _ = run(
        [{"email": "a@x", "allocation": 0.5}], D(2026, 11, 2), D(2026, 11, 13), rituals=RITUALS
    )
    columns = list(tc.COMPONENTS)
    assert (half.days[columns].sum() * 2).to_dict() == pytest.approx(
        full.days[columns].sum().to_dict()
    )


def test_unmapped_title_class_is_reported_not_dropped_silently():
    result, _ = run(
        [
            {"email": "a@x"},
            {"email": "i@x", "classified_title": "Intern"},
            {"email": "", "area_path": "P"},
            {"email": "n@x", "area_path": None},
        ],
        D(2026, 11, 2),
        D(2026, 11, 6),
    )
    assert [(p.email, p.problem) for p in result.problems] == [
        ("i@x", "no role group"),
        ("", "no email"),
        ("n@x", "no AreaPath"),
    ]
    assert set(result.days["email"]) == {"a@x"}


def test_unapproved_people_are_skipped():
    result, _ = run(
        [{"email": "a@x"}, {"email": "u@x", "approved": False}], D(2026, 11, 2), D(2026, 11, 6)
    )
    assert set(result.days["email"]) == {"a@x"}


def test_rates_apply_to_a_subtree_in_their_dates_and_a_deeper_row_wins():
    result, _ = run(
        [
            {"email": "a@x", "area_path": "Proj\\Dept\\Team"},
            {"email": "b@x", "area_path": "Proj\\Dept\\Other"},
            {"email": "c@x", "area_path": "Proj\\Else"},
        ],
        D(2026, 11, 2),
        D(2026, 11, 13),
        rates={
            "area_path": ["Proj\\Dept", "Proj\\Dept\\Team"],
            "from": [D(2026, 11, 9), None],
            "to": [D(2026, 11, 13), None],
            "phase": ["stabilization", ""],
            "bug_fix_quota": [0.8, 0.2],
            "focus_loss": [0.1, None],
        },
    )
    days = result.days.assign(
        quota=lambda d: d["bug_fix"] / (d["gross"] - d["ritual"] - d["illness"] - d["vacation"])
    ).set_index(["email", "date"])
    assert days.loc[("a@x", D(2026, 11, 2)), "quota"] == pytest.approx(0.2)
    assert days.loc[("a@x", D(2026, 11, 10)), "quota"] == pytest.approx(0.2)
    assert days.loc[("b@x", D(2026, 11, 2)), "quota"] == pytest.approx(0.5)
    assert days.loc[("b@x", D(2026, 11, 10)), "quota"] == pytest.approx(0.8)
    assert days.loc[("c@x", D(2026, 11, 10)), "quota"] == pytest.approx(0.5)
    # a@x's own row leaves focus loss blank, so it inherits the stabilization row.
    assert days.loc[("a@x", D(2026, 11, 10)), "focus"] == pytest.approx(8 * 0.1)
    assert days.loc[("a@x", D(2026, 11, 2)), "focus"] == pytest.approx(8 * 0.12)


def test_bug_fix_quota_applies_only_to_flagged_groups():
    result, _ = run(
        [{"email": "t@x", "classified_title": "testing engineer"}], D(2026, 11, 2), D(2026, 11, 6)
    )
    assert result.days["bug_fix"].sum() == 0
    assert result.days["net"].sum() == pytest.approx(40 * 0.88)


def test_joiner_counts_at_half_for_three_months():
    result, _ = run(
        [{"email": "j@x", "start_date": "2026-01-15"}], D(2026, 1, 1), D(2026, 5, 31), **NO_TAX
    )
    days = by_date(result)
    assert days.loc[D(2026, 4, 14), "net"] == 4
    assert days.loc[D(2026, 4, 15), "net"] == 8
    assert days.loc[D(2026, 1, 14), "gross"] == 0


def test_split_person_over_allocated_is_a_problem():
    result, _ = run(
        [
            {"email": "a@x", "allocation": 0.7},
            {"email": "a@x", "area_path": "P\\B", "allocation": 0.5},
        ],
        D(2026, 11, 2),
        D(2026, 11, 6),
    )
    assert [p.problem for p in result.problems] == ["allocation over 1"]


# ---------------------------------------------------------------------------
# Outputs and command line (CP-003)
# ---------------------------------------------------------------------------


def write(path, sheets):
    with pd.ExcelWriter(path) as writer:
        for name, frame in sheets.items():
            pd.DataFrame(frame).to_excel(writer, sheet_name=name, index=False)
    return path


@pytest.fixture
def files(tmp_path):
    staff = roster(
        {"email": "a@x"},
        {"email": "b@x", "classified_title": "team lead"},
        {"email": "c@x", "classified_title": "testing engineer", "area_path": "P\\B"},
        {"email": "d@x", "area_path": "P\\B\\C", "start_date": "2026-11-16"},
    )
    roster_path = write(tmp_path / "roster.xlsx", {"people": staff})
    config_path = tmp_path / "config.xlsx"
    assert tc.main(["--template", str(config_path)]) == 0
    return roster_path, config_path, tmp_path / "out" / "capacity.xlsx"


def cli(files, *extra):
    roster_path, config_path, output = files
    return tc.main(
        [
            "--roster",
            str(roster_path),
            "--config",
            str(config_path),
            "--start-month",
            "2026-11",
            "--months",
            "3",
            "--output",
            str(output),
            *extra,
        ]
    )


def test_output_sheets_and_sums(files):
    assert cli(files) == 0
    book = pd.read_excel(files[2], sheet_name=None)
    assert list(book) == [
        "dev_per_area_capacity",
        "capacity_by_role",
        "capacity_by_person",
        "calendar_weeks",
        "problems",
    ]
    capacity = book["dev_per_area_capacity"]
    assert capacity.columns.tolist() == ["AreaPath", "2026-11", "2026-12", "2027-01"]
    assert capacity["AreaPath"].tolist() == ["P\\A", "P\\B\\C"]  # P\B holds a tester only
    role, person = book["capacity_by_role"], book["capacity_by_person"]
    for frame in (role, person):
        assert frame["gross"].sum() > 0
    keys = ["area_path", "group", "period_type", "period"]
    from_people = person.groupby(keys)[list(tc.COMPONENTS)].sum()
    assert role.set_index(keys)[list(tc.COMPONENTS)].sort_index().to_numpy() == pytest.approx(
        from_people.sort_index().to_numpy()
    )
    pool = role[(role["period_type"] == "month") & role["group"].isin(["developer", "team lead"])]
    assert capacity.set_index("AreaPath").sum().sum() == pytest.approx(pool["net"].sum())
    assert book["problems"].empty


def test_ado_triage_reads_the_capacity_sheet(files):
    triage = pytest.importorskip("workflow_automations.ado_triage")
    assert cli(files) == 0
    person = pd.read_excel(files[2], sheet_name="capacity_by_person")
    pool = person[
        (person["period_type"] == "month")
        & person["group"].isin(["developer", "team lead"])
        & person["period"].isin(["2026-11", "2026-12"])
    ]
    expected = pool.groupby("area_path")["net"].sum().to_dict()
    assert expected["P\\A"] > 0 and expected["P\\B\\C"] > 0
    loaded = triage.load_capacity(files[2], ["2026-11", "2026-12"])
    assert loaded == pytest.approx(expected)


def test_existing_output_needs_overwrite(files):
    assert cli(files) == 0
    before = files[2].read_bytes()
    assert cli(files) == 1
    assert files[2].read_bytes() == before
    assert cli(files, "--overwrite") == 0


def test_output_may_not_replace_an_input(files):
    roster_path, config_path, _ = files
    before = config_path.read_bytes()
    assert cli((roster_path, config_path, config_path), "--overwrite") == 1
    assert config_path.read_bytes() == before


def test_problems_exit_non_zero_unless_allowed(files, tmp_path):
    roster_path, config_path, output = files
    write(
        roster_path,
        {"people": roster({"email": "a@x"}, {"email": "i@x", "classified_title": "Intern"})},
    )
    assert cli(files) == 3
    problems = pd.read_excel(output, sheet_name="problems")
    assert problems[["email", "problem"]].values.tolist() == [["i@x", "no role group"]]
    assert cli(files, "--overwrite", "--allow-problems") == 0


def test_input_error_writes_nothing(files):
    roster_path, config_path, output = files
    book = pd.read_excel(config_path, sheet_name=None)
    book["absences"] = pd.DataFrame(
        {"email": ["ghost@x"], "from": ["2026-11-02"], "to": ["2026-11-02"], "type": ["vacation"]}
    )
    write(config_path, book)
    assert cli(files) == 1
    assert not output.exists()


def test_missing_arguments_fail(files, capsys):
    assert tc.main(["--roster", str(files[0])]) == 1
    with pytest.raises(SystemExit) as exit_info:
        cli(files, "--months", "0")
    assert exit_info.value.code == 2


def test_template_is_a_valid_config_with_the_uae_calendar(files):
    _, config_path, _ = files
    book = pd.read_excel(config_path, sheet_name=None)
    config = tc.read_config(book, set())
    assert config.calendar[("UAE", D(2026, 12, 2))] == 0
    assert config.calendar[("UAE", D(2027, 3, 9))] == 0
    assert config.calendar[("UAE", D(2026, 3, 2))] == 6  # Ramadan
    assert ("UAE", D(2026, 3, 19)) in config.calendar
    assert config.countries["UAE"].working_ratio == pytest.approx(5 / 7)
    assert config.settings["iteration_start"] == D(2026, 1, 5)


def test_year_without_calendar_rows_is_a_problem(files):
    roster_path, config_path, output = files
    roster_args = [
        "--roster",
        str(roster_path),
        "--config",
        str(config_path),
        "--start-month",
        "2027-12",
        "--months",
        "2",
        "--output",
        str(output),
    ]
    assert tc.main(roster_args) == 3
    problems = pd.read_excel(output, sheet_name="problems")
    assert problems["detail"].tolist() == ["UAE has no calendar rows for 2028"]


def test_uae_reference_calendar_keeps_holidays_out_of_ramadan_short_days():
    rows = tc.uae_calendar_rows()
    days = [r["date"] for r in rows]
    assert len(days) == len(set(days))
    assert all(r["hours"] in (0, 6) for r in rows)
    assert {r["date"].year for r in rows} == {2026, 2027}


def test_december_leave_beyond_the_prorated_budget_is_not_a_problem():
    result, _ = run(
        [{"email": "a@x"}],
        D(2026, 12, 1),
        D(2026, 12, 31),
        settings={"vacation_days": 30},
        absences={
            "email": ["a@x"],
            "from": [D(2026, 12, 14)],
            "to": [D(2026, 12, 31)],
            "type": ["vacation"],
        },
    )
    assert result.problems == []
    assert result.days["vacation"].sum() == 0
    assert result.days["absence"].sum() == 13 * 8  # Dec 25 is a holiday


def test_people_and_fte_count_only_employed_days(files):
    roster_path, config_path, output = files
    staff = roster(
        {"email": "a@x", "end_date": "2026-11-02"},
        {"email": "b@x", "start_date": "2026-12-30", "ramp_up_months": 0},
    )
    write(roster_path, {"people": staff})
    assert cli(files) == 0
    role = pd.read_excel(output, sheet_name="capacity_by_role").set_index(["period_type", "period"])
    november, december = role.loc[("month", "2026-11")], role.loc[("month", "2026-12")]
    assert (november["people"], december["people"]) == (1, 1)
    # UAE November and December 2026 both have 21 working days (National Day Dec 2-3).
    assert november["fte"] == pytest.approx(1 / 21)
    assert december["fte"] == pytest.approx(2 / 21)


def test_inherited_rates_over_one_fail():
    with pytest.raises(tc.CapacityError, match="bug_fix_quota 0.95 \\+ focus_loss 0.12"):
        run(
            [{"email": "a@x"}],
            D(2026, 11, 2),
            D(2026, 11, 6),
            rates={"area_path": ["P"], "from": [None], "to": [None], "bug_fix_quota": [0.95]},
        )


def test_area_paths_differing_in_case_share_one_spelling():
    result, config = run(
        [{"email": "a@x", "area_path": "P\\A"}, {"email": "b@x", "area_path": "p\\a"}],
        D(2026, 11, 2),
        D(2026, 11, 6),
    )
    outputs = tc.build_outputs(result, config, D(2026, 11, 2), D(2026, 11, 6), ["2026-11"])
    assert outputs["dev_per_area_capacity"]["AreaPath"].tolist() == ["P\\A"]


def test_absences_from_after_the_window_start_fails():
    with pytest.raises(tc.CapacityError, match="absences_from 2026-12-01 is after"):
        run(
            [{"email": "a@x"}],
            D(2026, 11, 1),
            D(2026, 12, 31),
            settings={"absences_from": D(2026, 12, 1)},
        )


def test_ritual_with_blank_hours_or_groups_fails():
    with pytest.raises(tc.CapacityError) as error:
        run(
            [{"email": "a@x"}],
            D(2026, 11, 2),
            D(2026, 11, 6),
            rituals={
                "name": ["a", "b"],
                "per": ["day", "day"],
                "hours": [None, 1],
                "groups": ["all", None],
            },
        )
    assert "rituals row 2: hours is blank" in str(error.value)
    assert "rituals row 3: groups is blank" in str(error.value)


def test_no_triage_pool_is_a_problem():
    result, config = run(
        [{"email": "t@x", "classified_title": "testing engineer"}], D(2026, 11, 2), D(2026, 11, 6)
    )
    tc.build_outputs(result, config, D(2026, 11, 2), D(2026, 11, 6), ["2026-11"])
    assert [p.problem for p in result.problems] == ["no triage-pool people"]


def test_dates_are_written_without_a_time(files):
    import openpyxl

    sheet = openpyxl.load_workbook(files[1])["calendar"]
    assert sheet["B2"].number_format == "YYYY-MM-DD"


# ---------------------------------------------------------------------------
# Config from the command line (CP-015, CP-016)
# ---------------------------------------------------------------------------


def person_hours(output, email, period):
    person = pd.read_excel(output, sheet_name="capacity_by_person")
    rows = person[(person["email"] == email) & (person["period"] == period)]
    return rows.iloc[0]


def test_calendar_files_per_year_replace_the_config_calendar(files, tmp_path):
    # One CSV for 2026 (semicolons, as some Excel locales save it) and a workbook for 2027.
    (tmp_path / "cal-2026.csv").write_text(
        "country;date;hours;name\nUAE;2026-11-02;0;company day\n", encoding="utf-8-sig"
    )
    write(
        tmp_path / "cal-2027.xlsx",
        {"holidays": {"country": ["UAE"], "date": [D(2027, 1, 1)], "hours": [0]}},
    )
    args = ["--calendar", str(tmp_path / "cal-2026.csv"), "--calendar"]
    assert cli(files, *args, str(tmp_path / "cal-2027.xlsx")) == 0
    november = person_hours(files[2], "a@x", "2026-11")
    december = person_hours(files[2], "a@x", "2026-12")
    # 21 weekdays in November less the company day; National Day no longer applies.
    assert november["gross"] == pytest.approx(20 * 8)
    assert december["gross"] == pytest.approx(23 * 8)


def test_calendar_file_with_a_missing_year_is_a_problem(files, tmp_path):
    path = tmp_path / "cal.csv"
    path.write_text("country,date,hours\nUAE,2026-12-02,0\n", encoding="utf-8")
    assert cli(files, "--calendar", str(path)) == 3
    problems = pd.read_excel(files[2], sheet_name="problems")
    assert problems["detail"].tolist() == ["UAE has no calendar rows for 2027"]


def test_calendar_file_rows_are_validated(files, tmp_path):
    path = tmp_path / "cal.csv"
    path.write_text("country,date,hours\nUAE,2026-12-02,0\nXX,2027-01-01,0\n", encoding="utf-8")
    other = tmp_path / "cal2.csv"
    other.write_text("country,date,hours\nUAE,2026-12-02,0\n", encoding="utf-8")
    assert cli(files, "--calendar", str(path), "--calendar", str(other)) == 1
    assert not files[2].exists()


def test_template_calendar_round_trips_as_csv_and_xlsx(files, tmp_path):
    template = pd.read_excel(files[1], sheet_name=None)
    for name in ("uae.csv", "uae.xlsx"):
        path = tmp_path / name
        assert tc.main(["--template-calendar", str(path)]) == 0
        frame = tc.read_table(path, "calendar")
        config = tc.read_config({**template, "calendar": frame}, set())
        assert config.calendar == tc.read_config(template, set()).calendar
    assert tc.main(["--template-calendar", str(tmp_path / "uae.csv")]) == 1  # exists


def test_set_overrides_the_settings_sheet(files):
    assert cli(files, "--set", "bug_fix_quota=0", "--set", "FOCUS_LOSS=0") == 0
    row = person_hours(files[2], "a@x", "2026-11")
    assert row["bug_fix"] == 0 and row["focus"] == 0 and row["net"] > 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--set", "bug_fix_quota"], "is not KEY=VALUE"),
        (["--set", "colour=red"], "--set colour: unknown setting"),
        (["--set", "focus_loss=0.9"], "bug_fix_quota + focus_loss is more than 1"),
        (["--set", "iteration_start=2026-01-06"], "is not a Monday"),
        (["--sheet", "holidays=x.csv"], "--sheet holidays: unknown sheet"),
        (["--sheet", "absences=missing.csv"], "absences file does not exist"),
    ],
)
def test_bad_overrides_fail_with_their_flag(files, args, message, caplog):
    assert cli(files, *args) == 1
    assert message in caplog.text
    assert not files[2].exists()


def test_sheet_file_replaces_a_config_sheet(files, tmp_path):
    path = tmp_path / "leave.csv"
    path.write_text("email,from,to,type\na@x,2026-11-02,2026-11-06,vacation\n", encoding="utf-8")
    assert cli(files, "--sheet", f"absences={path}") == 0
    row = person_hours(files[2], "a@x", "2026-11")
    assert row["absence"] == pytest.approx(5 * 8)


def test_workbook_with_several_sheets_needs_one_named_for_the_sheet(files, tmp_path):
    path = write(
        tmp_path / "two.xlsx",
        {
            "a": {"country": ["UAE"], "date": [D(2027, 1, 1)], "hours": [0]},
            "b": {"country": ["UAE"], "date": [D(2027, 1, 2)], "hours": [0]},
        },
    )
    assert cli(files, "--calendar", str(path)) == 1
    with pytest.raises(tc.CapacityError, match="none is named calendar"):
        tc.read_table(path, "calendar")


def test_output_may_not_replace_a_calendar_file(files, tmp_path):
    path = tmp_path / "cal.csv"
    path.write_text("country,date,hours\nUAE,2026-12-02,0\nUAE,2027-01-01,0\n", encoding="utf-8")
    before = path.read_bytes()
    roster_path, config_path, _ = files
    args = ["--calendar", str(path), "--overwrite"]
    assert cli((roster_path, config_path, path), *args) == 1
    assert path.read_bytes() == before


def test_stacked_files_align_columns_whatever_their_case(files, tmp_path):
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    # Settings files replace the whole sheet, so the first one carries the cadence.
    first.write_text(
        "KEY,VALUE\nbug_fix_quota,0\niteration_start,2026-01-05\ndefault_country,UAE\n",
        encoding="utf-8",
    )
    second.write_text(" key ,value\nfocus_loss,0\n", encoding="utf-8")
    assert cli(files, "--sheet", f"settings={first}", "--sheet", f"settings={second}") == 0
    row = person_hours(files[2], "a@x", "2026-11")
    assert row["bug_fix"] == 0 and row["focus"] == 0


def test_repeated_column_in_a_file_fails(tmp_path):
    path = tmp_path / "cal.csv"
    path.write_text("country,Country,date,hours\nUAE,UAE,2027-01-01,0\n", encoding="utf-8")
    with pytest.raises(tc.CapacityError, match=r"columns \['country'\] appear more than once"):
        tc.read_table(path, "calendar")


@pytest.mark.parametrize("text", ["country\nUAE\n", "country\tworkdays\nUAE\tMon-Fri\n"])
def test_csv_separator_is_comma_semicolon_or_tab_only(files, tmp_path, text):
    path = tmp_path / "countries.csv"
    path.write_text(text, encoding="utf-8")
    assert tc.read_table(path, "countries").columns.tolist()[0] == "country"
    assert cli(files, "--sheet", f"countries={path}") == 0
