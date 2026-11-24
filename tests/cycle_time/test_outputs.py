"""Input validation, the partition check, trace nesting, percentiles and the CLI."""

import csv
import json
import time
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
        (
            {"work_items": [{"id": 1, "states": [None]}]},
            r"work_items\[0\]\.states\[0\]: must be an object",
        ),
        ({"work_items": "x"}, "must be a list"),
        ({"topics": [{"id": "t", "changes": [None]}]}, r"changes\[0\]: must be an object"),
        ({"topics": [{"id": "t", "changes": [{"number": 1, "events": [5]}]}]}, r"events\[0\]"),
        ({"topics": [{"id": "t", "builds": "x"}]}, r"topics\[0\]\.builds: must be a list"),
        ({"topics": [None]}, r"topics\[0\]: must be an object"),
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
    expected = sum(len(json.loads(p.read_text())["expected"]) for p in SCENARIOS)
    assert len(cycles) == expected
    names = {e["name"] for e in trace["traceEvents"] if e.get("cat") == "span"}
    assert {"coding", "rework", "review_wait", "ci_run", "unattributed"} <= names


def test_percentile_interpolates_linearly():
    assert report.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert report.percentile([10], 0.95) == 10
    assert report.percentile([0, 10], 0.95) == pytest.approx(9.5)


def test_summary_leaves_out_open_cycles():
    rows = report.summary(all_results())
    tools_cycle = next(r for r in rows if r["team"] == "Tools" and r["kind"] == "cycle")
    # 107, 108, 110, 113, 114, 122, 123, 126; 109 and 115 are open
    assert tools_cycle["work_items"] == 8


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


def scenario(stem):
    return next(p for p in SCENARIOS if p.stem.startswith(stem))


def test_overlapping_builds_get_separate_lanes_and_run_to_the_cutoff():
    results = compute(model.read(scenario("15_")))
    trace = perfetto.export(results)
    lanes = {
        e["tid"]: e["args"]["name"] for e in trace["traceEvents"] if e["name"] == "thread_name"
    }
    ci = [e for e in trace["traceEvents"] if e.get("cat") == "ci"]
    by_build = defaultdict(list)
    for e in ci:
        by_build[e["args"]["build"]].append(e)
    assert lanes[by_build[11501][0]["tid"]] != lanes[by_build[11502][0]["tid"]]
    cutoff_us = model.parse_time("2026-11-02T13:00:00Z", "") * perfetto.US
    running = next(e for e in by_build[11502] if e["name"].startswith("build"))
    assert running["name"] == "build 11502 running" and running["args"]["ongoing"]
    assert running["ts"] + running["dur"] == cutoff_us
    (queued,) = by_build[11503]
    assert queued["name"] == "queued" and queued["args"]["ongoing"]
    assert queued["ts"] + queued["dur"] == cutoff_us


def test_each_file_keeps_its_own_cutoff():
    open_monday = model.read(scenario("09_"))  # as_of 2026-11-02T18:00Z
    later = model.read(scenario("01_"))  # as_of 2026-11-06
    (alone,) = [r for r in compute(open_monday) if r.item.id == 109]
    merged = next(r for r in compute(model.merge([open_monday, later])) if r.item.id == 109)
    assert merged.cycle_end == alone.cycle_end == model.parse_time("2026-11-02T18:00Z", "")
    assert merged.spans == alone.spans


def test_critical_topic_ignores_activity_after_close():
    data = model.load(
        minimal(
            work_items=[
                {
                    "id": 1,
                    "states": [
                        {"rev": 1, "state": "Active", "at": "2026-11-02T09:00Z"},
                        {"rev": 2, "state": "Closed", "at": "2026-11-02T12:00Z"},
                    ],
                }
            ],
            topics=[
                {
                    "id": "merged",
                    "wi_ids": [1],
                    "changes": [
                        {
                            "number": 1,
                            "events": [
                                {"kind": "patchset", "patchset": 1, "at": "2026-11-02T09:30Z"},
                                {"kind": "merged", "at": "2026-11-02T11:00Z"},
                            ],
                        }
                    ],
                },
                {
                    "id": "late",
                    "wi_ids": [1],
                    "changes": [
                        {
                            "number": 2,
                            "events": [
                                {"kind": "patchset", "patchset": 1, "at": "2026-11-02T09:40Z"}
                            ],
                        }
                    ],
                    "builds": [
                        {
                            "id": 9,
                            "queued_at": "2026-11-02T11:30Z",
                            "started_at": "2026-11-02T12:30Z",
                            "finished_at": "2026-11-02T13:00Z",
                            "result": "pass",
                        }
                    ],
                },
            ],
        )
    )
    (result,) = compute(data)
    assert result.critical_topic == "merged"


def test_report_csv_quotes_team_names(tmp_path):
    raw = json.loads(scenario("01_").read_text())
    raw["work_items"][0]["team"] = "Platform, Core"
    path = tmp_path / "in.json"
    path.write_text(json.dumps(raw))
    out = tmp_path / "report.csv"
    assert main(["report", str(path), "--csv", "-o", str(out)]) == 0
    rows = list(csv.reader(out.read_text().splitlines()))
    assert all(len(row) == 5 for row in rows)
    assert rows[1][0] == "Platform, Core"


def test_cli_leaves_neighbouring_files_alone(tmp_path):
    out = tmp_path / "report.csv"
    neighbour = tmp_path / "report.csv.tmp"
    neighbour.write_text("keep me")
    assert main(["report", str(SCENARIOS[0]), "--csv", "-o", str(out)]) == 0
    assert neighbour.read_text() == "keep me"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.csv", "report.csv.tmp"]


def test_tested_must_map_changes_to_patchsets():
    topic = {
        "id": "t",
        "wi_ids": [1],
        "builds": [{"id": 1, "queued_at": "2026-11-02T10:00Z", "tested": {"abc": 1}}],
    }
    with pytest.raises(model.InputError, match="change number"):
        model.load(minimal(topics=[topic]))


def test_cli_exits_2_on_malformed_nested_records(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(minimal(work_items=[{"id": 1, "states": [None]}])))
    assert main(["spans", str(bad)]) == 2
    assert "states[0]" in capsys.readouterr().err


def test_shared_topic_is_flagged_from_any_linked_topic():
    raw = json.loads(scenario("06_").read_text())
    raw["work_items"].append(
        {"id": 999, "states": [{"rev": 1, "state": "Active", "at": "2026-11-02T09:00Z"}]}
    )
    raw["topics"][0]["wi_ids"] = [106, 999]  # the non-critical topic is shared
    results = {r.item.id: r for r in compute(model.load(raw))}
    assert results[106].critical_topic == "t106b"
    assert {"multi_topic", "shared_topic"} <= results[106].flags


def test_early_build_queue_counts_as_activity_before_active():
    raw = json.loads(scenario("01_").read_text())
    raw["topics"][0]["builds"][0]["queued_at"] = "2026-11-02T06:00:00Z"
    (result,) = compute(model.load(raw))
    assert "activity_before_active" in result.flags
    assert result.metrics["activity_before_active_s"] == 3 * 3600


def test_aligned_trace_clips_ci_phases_at_the_cycle_start():
    raw = json.loads(scenario("01_").read_text())
    # Active at 10:25 on day two: between the 10:20 queue and the 10:30 build start.
    raw["work_items"][0]["states"][1]["at"] = "2026-11-03T10:25:00Z"
    raw["work_items"][0]["states"][2]["at"] = "2026-11-03T10:25:00Z"
    results = compute(model.load(raw))
    kinds = [s.kind for s in results[0].spans]
    assert "ci_queue" in kinds and "ci_run" in kinds
    ci = [e for e in perfetto.export(results, "aligned")["traceEvents"] if e.get("cat") == "ci"]
    queued = next(e for e in ci if e["name"] == "queued")
    assert (
        queued["ts"] == 0 and queued["dur"] == 5 * 60 * model.SECOND and queued["args"]["clipped"]
    )
    assert any(e["name"] == "build 10101 pass" for e in ci)


def test_many_builds_compute_in_linear_time():
    # Sequential builds used to be rescanned at every cut (quadratic): 400 took 0.26 s.
    base = model.parse_time("2026-11-02T10:00:00Z", "")
    builds = []
    for k in range(3000):
        q = base + k * 600 * model.SECOND
        builds.append(
            {
                "id": k,
                "queued_at": model.format_time(q),
                "started_at": model.format_time(q + 60 * model.SECOND),
                "finished_at": model.format_time(q + 300 * model.SECOND),
                "result": "pass" if k % 2 else "infra",
            }
        )
    data = model.load(
        minimal(
            topics=[
                {
                    "id": "t",
                    "wi_ids": [1],
                    "changes": [
                        {
                            "number": 1,
                            "events": [
                                {"kind": "patchset", "patchset": 1, "at": "2026-11-02T09:10Z"},
                                {"kind": "ready", "at": "2026-11-02T09:30Z"},
                                {"kind": "vote", "value": 2, "at": "2026-11-02T09:40Z"},
                            ],
                        }
                    ],
                    "builds": builds,
                }
            ],
            as_of="2026-11-30T00:00:00Z",
        )
    )
    started = time.perf_counter()
    (result,) = compute(data)
    assert time.perf_counter() - started < 3.0
    assert sum(s.kind == "ci_run" for s in result.spans) == 3000


def test_critical_topic_prefers_a_completed_topic_over_a_partly_merged_one():
    def change(number, *events):
        return {"number": number, "events": [dict(e) for e in events]}

    ps = {"kind": "patchset", "patchset": 1}
    data = model.load(
        minimal(
            work_items=[
                {
                    "id": 1,
                    "states": [
                        {"rev": 1, "state": "Active", "at": "2026-11-02T00:00Z"},
                        {"rev": 2, "state": "Closed", "at": "2026-11-02T01:00Z"},
                    ],
                }
            ],
            topics=[
                {
                    "id": "done",
                    "wi_ids": [1],
                    "changes": [
                        change(
                            1,
                            {**ps, "at": "2026-11-02T00:05Z"},
                            {"kind": "merged", "at": "2026-11-02T00:15Z"},
                        )
                    ],
                },
                {
                    "id": "half",
                    "wi_ids": [1],
                    "changes": [
                        change(
                            2,
                            {**ps, "at": "2026-11-02T00:06Z"},
                            {"kind": "merged", "at": "2026-11-02T00:20Z"},
                        ),
                        change(
                            3,
                            {**ps, "at": "2026-11-02T00:07Z"},
                            {"kind": "ready", "at": "2026-11-02T00:25Z"},
                        ),
                    ],
                },
            ],
        )
    )
    (result,) = compute(data)
    assert result.critical_topic == "done"


def test_stale_builds_compute_in_linear_time():
    # Builds of an outdated patchset are retired, not kept and rescanned at every cut.
    base = model.parse_time("2026-11-02T10:00:00Z", "")
    builds = []
    for k in range(3000):
        q = base + k * 600 * model.SECOND
        builds.append(
            {
                "id": k,
                "queued_at": model.format_time(q),
                "started_at": model.format_time(q + 60 * model.SECOND),
                "finished_at": model.format_time(q + 300 * model.SECOND),
                "result": "pass",
                "tested": {"1": 1},
            }
        )
    data = model.load(
        minimal(
            topics=[
                {
                    "id": "t",
                    "wi_ids": [1],
                    "changes": [
                        {
                            "number": 1,
                            "events": [
                                {"kind": "patchset", "patchset": 1, "at": "2026-11-02T09:10Z"},
                                {"kind": "patchset", "patchset": 2, "at": "2026-11-02T09:20Z"},
                                {"kind": "ready", "at": "2026-11-02T09:30Z"},
                                {"kind": "vote", "value": 2, "at": "2026-11-02T09:40Z"},
                            ],
                        }
                    ],
                    "builds": builds,
                }
            ],
            as_of="2026-11-30T00:00:00Z",
        )
    )
    started = time.perf_counter()
    (result,) = compute(data)
    assert time.perf_counter() - started < 3.0
    assert {s.kind for s in result.spans} == {"coding", "review_wait", "unattributed"}


def test_critical_topic_ranks_by_completion_transition():
    # A became complete at 00:20 (abandon of its open sibling), B at 00:12: A is critical,
    # although A's last merge (00:07) is earlier than B's.
    def change(number, *events):
        return {"number": number, "events": [dict(e) for e in events]}

    ps = {"kind": "patchset", "patchset": 1}
    data = model.load(
        minimal(
            work_items=[
                {
                    "id": 1,
                    "states": [
                        {"rev": 1, "state": "Active", "at": "2026-11-02T00:00Z"},
                        {"rev": 2, "state": "Closed", "at": "2026-11-02T00:30Z"},
                    ],
                }
            ],
            topics=[
                {
                    "id": "A",
                    "wi_ids": [1],
                    "changes": [
                        change(
                            1,
                            {**ps, "at": "2026-11-02T00:01Z"},
                            {"kind": "merged", "at": "2026-11-02T00:07Z"},
                        ),
                        change(
                            2,
                            {**ps, "at": "2026-11-02T00:03Z"},
                            {"kind": "abandoned", "at": "2026-11-02T00:20Z"},
                        ),
                    ],
                },
                {
                    "id": "B",
                    "wi_ids": [1],
                    "changes": [
                        change(
                            3,
                            {**ps, "at": "2026-11-02T00:02Z"},
                            {"kind": "merged", "at": "2026-11-02T00:12Z"},
                        )
                    ],
                },
            ],
        )
    )
    (result,) = compute(data)
    assert result.critical_topic == "A"


def test_spans_csv_keeps_sub_second_times():
    out = report.spans_csv(compute(model.read(scenario("20_"))))
    assert "2026-11-02T10:04:00.100000Z" in out and ",0.8," in out
