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
        "tier": "routine",
    }
    fields.update(extra)
    front = "\n".join(f"{key}: {value}" for key, value in fields.items())
    (directory / f"{id_}.md").write_text(f"---\n{front}\n---\n\n# {id_}\n")


def test_repository_backlog_is_valid():
    tasks = backlog.load_tasks()
    assert tasks, "docs/backlog has no task files"
    assert backlog.main(["check"]) == 0


@pytest.fixture
def backlog_dir(tmp_path):
    """A repository root with docs/backlog and one specification with nested sections."""
    directory = tmp_path / "docs" / "backlog"
    directory.mkdir(parents=True)
    (tmp_path / "docs" / "spec.md").write_text(
        "# Spec\n\n## Export CLI\n\ntext\n\n### Traversal\n\ntext\n\n"
        "### Exported data\n\ntext\n\n## Analyze\n\ntext\n"
    )
    return directory


def test_invalid_task_is_rejected(backlog_dir):
    write_task(backlog_dir, "AG-001", "a.py", tier="unknown")
    with pytest.raises(ValueError, match="tier must be one of"):
        backlog.load_tasks(backlog_dir)
    assert backlog.main(["check", "--dir", str(backlog_dir)]) == 1


def test_id_must_match_file_name(backlog_dir):
    write_task(backlog_dir, "AG-001", "a.py")
    (backlog_dir / "AG-001.md").rename(backlog_dir / "AG-002.md")
    with pytest.raises(ValueError, match="match the file name"):
        backlog.load_tasks(backlog_dir)


def test_unknown_section_anchor_is_rejected(backlog_dir):
    write_task(backlog_dir, "AG-001", "docs/spec.md#no-such-heading")
    with pytest.raises(ValueError, match="no heading with anchor"):
        backlog.load_tasks(backlog_dir)


@pytest.mark.parametrize(
    "deps",
    [
        {"AG-001": "AG-002", "AG-002": "AG-001"},
        {"AG-001": "AG-002", "AG-002": "AG-003", "AG-003": "AG-001"},
    ],
)
def test_dependency_cycles_are_rejected(backlog_dir, deps):
    for id_, dep in deps.items():
        write_task(backlog_dir, id_, f"{id_}.py", deps=dep)
    with pytest.raises(ValueError, match="dependency cycle: AG-001 -> .* -> AG-001"):
        backlog.load_tasks(backlog_dir)
    assert backlog.main(["check", "--dir", str(backlog_dir)]) == 1


@pytest.mark.parametrize("touch", ["/etc/passwd", "../outside.py", "src/../../x.py"])
def test_paths_outside_the_repository_are_rejected(backlog_dir, touch):
    write_task(backlog_dir, "AG-001", touch)
    with pytest.raises(ValueError, match="inside the repository"):
        backlog.load_tasks(backlog_dir)


def spans(backlog_dir, *touches):
    return [backlog.resolve_touch(t, backlog_dir.parents[1]) for t in touches]


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("src/x.py", "src/x.py", True),
        ("src/pkg/", "src/pkg/a.py", True),
        ("src/pkg/a.py", "src/pkg/b.py", False),
        ("docs/spec.md#traversal", "docs/spec.md#exported-data", False),
        ("docs/spec.md#export-cli", "docs/spec.md#analyze", False),
        ("docs/spec.md#export-cli", "docs/spec.md#traversal", True),
        ("docs/spec.md#analyze", "docs/spec.md#analyze", True),
        ("docs/spec.md", "docs/spec.md#analyze", True),
        ("docs/new.md#later", "docs/new.md#other", True),
        ("./src/x.py", "src/pkg/../x.py", True),
        ("src/pkg", "src/pkg/", True),
    ],
)
def test_touch_conflicts(backlog_dir, a, b, expected):
    x, y = spans(backlog_dir, a, b)
    assert backlog.touches_conflict(x, y) is expected
    assert backlog.touches_conflict(y, x) is expected


def test_done_dependencies_are_satisfied_and_open_ones_block(backlog_dir):
    write_task(backlog_dir, "AG-002", "a.py", deps="AG-001")  # AG-001 deleted: done
    write_task(backlog_dir, "AG-003", "b.py", deps="AG-002")
    assert [t.id for t in backlog.ready(backlog.load_tasks(backlog_dir))] == ["AG-002"]


def ids(waves):
    return [[t.id for t in wave] for wave in waves]


def test_plan_separates_conflicting_tasks(backlog_dir):
    write_task(backlog_dir, "AG-001", "src/m.py, docs/spec.md#traversal")
    write_task(backlog_dir, "AG-002", "src/m.py")
    write_task(backlog_dir, "AG-003", "src/n.py, docs/spec.md#exported-data")
    write_task(backlog_dir, "AG-004", "src/o.py, docs/spec.md#export-cli")
    tasks = backlog.load_tasks(backlog_dir)
    assert ids(backlog.plan(tasks, set())) == [["AG-001", "AG-003"], ["AG-002", "AG-004"]]


def test_tasks_conflicting_with_busy_ones_wait(backlog_dir):
    write_task(backlog_dir, "AG-001", "src/m.py")
    write_task(backlog_dir, "AG-002", "src/m.py")
    write_task(backlog_dir, "AG-003", "src/n.py")
    tasks = backlog.load_tasks(backlog_dir)
    assert ids(backlog.plan(tasks, {"AG-001"})) == [["AG-003"]]
    assert [t.id for t in backlog.waiting_on_busy(tasks, {"AG-001"})] == ["AG-002"]


def test_plan_is_empty_when_every_ready_task_waits(backlog_dir, capsys):
    write_task(backlog_dir, "AG-001", "src/same.py")
    write_task(backlog_dir, "AG-002", "src/same.py")
    tasks = backlog.load_tasks(backlog_dir)
    assert backlog.plan(tasks, {"AG-001"}) == []
    assert backlog.main(["plan", "--busy", "AG-001", "--dir", str(backlog_dir)]) == 0
    out = capsys.readouterr().out
    assert "Wave" not in out
    assert "Waiting for in-progress tasks to merge:\n  AG-002" in out
