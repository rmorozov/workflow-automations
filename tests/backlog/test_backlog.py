"""Backlog task files stay valid and the parallel planner keeps conflicting tasks apart."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("backlog", ROOT / "scripts" / "backlog.py")
backlog = importlib.util.module_from_spec(_spec)
sys.modules["backlog"] = backlog  # dataclasses resolve their module by name
_spec.loader.exec_module(backlog)


def write_task(directory: Path, id_: str, touches: str, deps: str = "", **extra: str) -> None:
    fields = {
        "id": id_,
        "title": f"Task {id_}",
        "tool": "demo",
        "priority": "P2",
        "depends_on": f"[{deps}]",
        "touches": f"[{touches}]",
        "model": "sonnet",
        "effort": "medium",
    }
    fields.update(extra)
    front = "\n".join(f"{key}: {value}" for key, value in fields.items())
    (directory / f"{id_}.md").write_text(f"---\n{front}\n---\n\n# {id_}\n")


def test_repository_backlog_is_valid():
    tasks = backlog.load_tasks()
    assert tasks, "docs/backlog has no task files"
    assert backlog.main(["check"]) == 0


def test_invalid_task_is_rejected(tmp_path):
    write_task(tmp_path, "AG-001", "a.py", model="gpt")
    with pytest.raises(ValueError, match="model must be one of"):
        backlog.load_tasks(tmp_path)
    assert backlog.main(["check", "--dir", str(tmp_path)]) == 1


def test_id_must_match_file_name(tmp_path):
    write_task(tmp_path, "AG-001", "a.py")
    (tmp_path / "AG-001.md").rename(tmp_path / "AG-002.md")
    with pytest.raises(ValueError, match="match the file name"):
        backlog.load_tasks(tmp_path)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("src/x.py", "src/x.py", True),
        ("src/pkg/", "src/pkg/a.py", True),
        ("src/pkg/a.py", "src/pkg/b.py", False),
        ("docs/spec.md#export", "docs/spec.md#analyze", False),
        ("docs/spec.md#export", "docs/spec.md#export", True),
        ("docs/spec.md", "docs/spec.md#export", True),
    ],
)
def test_touch_conflicts(a, b, expected):
    assert backlog.touches_conflict(a, b) is expected
    assert backlog.touches_conflict(b, a) is expected


def test_done_dependencies_are_satisfied_and_open_ones_block(tmp_path):
    write_task(tmp_path, "AG-002", "a.py", deps="AG-001")  # AG-001 deleted: done
    write_task(tmp_path, "AG-003", "b.py", deps="AG-002")
    assert [t.id for t in backlog.ready(backlog.load_tasks(tmp_path))] == ["AG-002"]


def test_plan_separates_conflicting_tasks_and_respects_busy(tmp_path):
    write_task(tmp_path, "AG-001", "src/m.py, docs/s.md#one")
    write_task(tmp_path, "AG-002", "src/m.py")
    write_task(tmp_path, "AG-003", "src/n.py, docs/s.md#two")
    write_task(tmp_path, "AG-004", "src/o.py")
    tasks = backlog.load_tasks(tmp_path)

    waves = [[t.id for t in wave] for wave in backlog.plan(tasks, set())]
    assert waves == [["AG-001", "AG-003", "AG-004"], ["AG-002"]]

    waves = [[t.id for t in wave] for wave in backlog.plan(tasks, {"AG-001"})]
    assert waves == [["AG-003", "AG-004"], ["AG-002"]]
