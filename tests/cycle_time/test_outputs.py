"""Input validation, the partition check, trace nesting, percentiles and the CLI."""

import json
from collections import defaultdict
from pathlib import Path

import pytest

from workflow_automations.cycle_time import model, perfetto, report
from workflow_automations.cycle_time.cli import main
from workflow_automations.cycle_time.spans import PartitionError, Result, Span, compute, validate

SCENARIOS = sorted((Path(__file__).parent / "scenarios").glob("*.json"))


def all_results():
    return compute(model.merge([model.read(p) for p in SCENARIOS]))


def minimal(**over):
    data = {
        "schema": model.SCHEMA,
        "version": 1,
        "as_of": "2026-11-06T00:00:00Z",
        "work_items": [
            {"id": 1, "states": [{"rev": 1, "state": "Active", "at": "2026-11-02T09:00:00Z"}]}
        ],
        "topics": [],
    }
    data.update(over)
    return data


@pytest.mark.parametrize(
    "change, message",
    [
        ({"schema": "other"}, "schema"),
        ({"version": 2}, "version"),
        ({"as_of": "2026-11-06T00:00:00"}, "time zone"),
        (
            {
                "work_items": [
                    {"id": 1, "states": [{"rev": 1, "state": "Done", "at": "2026-11-02T09:00Z"}]}
                ]
            },
            "state",
        ),
        ({"work_items": [{"id": 1, "states": []}]}, "at least one"),
        (
            {
                "work_items": [
                    {
                        "id": True,
                        "states": [{"rev": 1, "state": "Active", "at": "2026-11-02T09:00Z"}],
                    }
                ]
            },
            "integer",
        ),
    ],
)
def test_invalid_input_is_refused(change, message):
    with pytest.raises(model.InputError, match=message):
        model.load(minimal(**change))


def test_build_times_must_be_ordered():
    topic = {
        "id": "t",
        "wi_ids": [1],
        "builds": [
            {
                "id": 1,
                "queued_at": "2026-11-02T10:00Z",
                "started_at": "2026-11-02T09:00Z",
                "finished_at": None,
                "result": None,
            }
        ],
    }
    with pytest.raises(model.InputError, match="queued_at <= started_at"):
        model.load(minimal(topics=[topic]))


def test_states_follow_revision_order_not_timestamps():
    # Two ADO changes in the same second: rev decides that Resolved came last.
    wi = {
        "id": 1,
        "states": [
            {"rev": 2, "state": "Resolved", "at": "2026-11-02T10:00Z"},
            {"rev": 1, "state": "Active", "at": "2026-11-02T10:00Z"},
        ],
    }
    data = model.load(minimal(work_items=[wi]))
    assert [s.state for s in data.work_items[1].states] == ["Active", "Resolved"]


def test_unknown_work_item_link_is_a_warning():
    data = model.load(minimal(topics=[{"id": "t", "wi_ids": [99]}]))
    assert "99" in data.warnings[0]


def test_partition_check_rejects_gaps():
    item = model.load(minimal()).work_items[1]
    result = Result(
        item,
        cycle_start=0,
        cycle_end=100,
        spans=[Span("coding", 1, 0, 40), Span("gate", 1, 50, 100)],
    )
    with pytest.raises(PartitionError):
        validate(result)


def test_merge_refuses_colliding_ids():
    part = model.read(SCENARIOS[0])
    with pytest.raises(model.InputError, match="more than one input"):
        model.merge([part, model.read(SCENARIOS[0])])


@pytest.mark.parametrize("mode", perfetto.MODES)
def test_trace_slices_nest_strictly_per_lane(mode):
    trace = perfetto.export(all_results(), mode)
    lanes = defaultdict(list)
    for event in trace["traceEvents"]:
        if event["ph"] == "X":
            assert event["dur"] >= 0
            if mode == "aligned":
                assert event["ts"] >= 0
            lanes[(event["pid"], event["tid"])].append((event["ts"], event["ts"] + event["dur"]))
    for slices in lanes.values():
        stack = []
        for start, end in sorted(slices, key=lambda s: (s[0], -s[1])):
            while stack and stack[-1] <= start:
                stack.pop()
            assert not stack or end <= stack[-1], "partial overlap on one lane"
            stack.append(end)


def test_trace_has_one_cycle_per_started_item_and_spans_at_depth_two():
    trace = perfetto.export(all_results())
    cycles = [e for e in trace["traceEvents"] if e.get("cat") == "cycle"]
    assert len(cycles) == 10
    names = {e["name"] for e in trace["traceEvents"] if e.get("cat") == "span"}
    assert {"coding", "rework", "review_wait", "ci_run", "unattributed"} <= names


def test_percentile_interpolates_linearly():
    assert report.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert report.percentile([10], 0.95) == 10
    assert report.percentile([0, 10], 0.95) == pytest.approx(9.5)


def test_summary_leaves_out_open_cycles():
    rows = report.summary(all_results())
    tools_cycle = next(r for r in rows if r["team"] == "Tools" and r["kind"] == "cycle")
    assert tools_cycle["work_items"] == 3  # 107, 108, 110; 109 is open


def test_cli_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "trace.json"
    assert main(["trace", str(SCENARIOS[0]), "-o", str(out)]) == 0
    assert json.loads(out.read_text())["otherData"]["mode"] == "calendar"
    assert main(["trace", str(SCENARIOS[0]), "-o", str(out)]) == 2
    assert "--overwrite" in capsys.readouterr().err
    assert (
        main(["trace", str(SCENARIOS[0]), "-o", str(out), "--overwrite", "--mode", "aligned"]) == 0
    )


def test_cli_spans_csv(capsys):
    assert main(["spans", str(SCENARIOS[2])]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("wi_id,team")
    assert any("rework,2" in line and "inferred_patchset" in line for line in lines)


def test_cli_reports_bad_input(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert main(["report", str(bad)]) == 2
    assert "schema" in capsys.readouterr().err
