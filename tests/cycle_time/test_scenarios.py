"""Every synthetic scenario's spans, flags and metrics match its hand-derived expectations."""

import json
from pathlib import Path

import pytest

from workflow_automations.cycle_time import model
from workflow_automations.cycle_time.spans import compute

SCENARIOS = sorted((Path(__file__).parent / "scenarios").glob("*.json"))


def actual_spans(result):
    rows = []
    for s in result.spans:
        row = [s.kind, s.round, model.format_time(s.start), model.format_time(s.end)]
        if s.flags:
            row.append(list(s.flags))
        rows.append(row)
    return rows


def normalize(rows):
    return [
        [
            r[0],
            r[1],
            model.format_time(model.parse_time(r[2], "")),
            model.format_time(model.parse_time(r[3], "")),
            *r[4:],
        ]
        for r in rows
    ]


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    results = {r.item.id: r for r in compute(model.load(raw))}
    assert set(map(str, results)) == set(raw["expected"])
    for wi_id, want in raw["expected"].items():
        result = results[int(wi_id)]
        assert actual_spans(result) == normalize(want["spans"]), wi_id
        assert sorted(result.flags) == sorted(want["flags"]), wi_id
        queue = [model.format_time(t) for t in result.queue] if result.queue else None
        want_queue = (
            [model.format_time(model.parse_time(t, "")) for t in want["queue"]]
            if want["queue"]
            else None
        )
        assert queue == want_queue, wi_id
        for key, value in want.get("metrics", {}).items():
            assert result.metrics.get(key) == value, (wi_id, key)
        if "critical_topic" in want:
            assert result.critical_topic == want["critical_topic"]


def test_scenarios_exist():
    assert len(SCENARIOS) >= 10
