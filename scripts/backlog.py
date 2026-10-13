"""List backlog tasks and plan which ones can run in parallel without conflicts.

Repository maintenance helper, not a packaged tool. Each task is one file,
``docs/backlog/<ID>.md``, with a small front matter block (see
``docs/backlog/README.md``). A task is done when its file is deleted, so a
dependency on a missing ID counts as satisfied.

    python scripts/backlog.py               # all open tasks
    python scripts/backlog.py ready         # tasks whose dependencies are done
    python scripts/backlog.py plan --busy AG-018
                                            # parallel waves of ready tasks
    python scripts/backlog.py check         # validate the task files
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

BACKLOG = Path(__file__).resolve().parent.parent / "docs" / "backlog"
ID_RE = re.compile(r"^[A-Z]{2}-\d{3}$")
REQUIRED = ("id", "title", "tool", "priority", "depends_on", "touches", "model", "effort")
PRIORITIES = ("P1", "P2", "P3")
MODELS = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    tool: str
    priority: str
    depends_on: tuple[str, ...]
    touches: tuple[str, ...]
    model: str
    effort: str
    path: Path


def parse_front_matter(text: str) -> dict[str, str | list[str]]:
    """Read ``key: value`` and ``key: [a, b]`` lines between ``---`` fences."""
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("missing front matter fences")
    end = lines.index("---", 1)
    fields: dict[str, str | list[str]] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(": ")
        if not sep:
            raise ValueError(f"not a key: value line: {line!r}")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
        else:
            fields[key] = value
    return fields


def load_task(path: Path) -> Task:
    fields = parse_front_matter(path.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED if key not in fields]
    if missing:
        raise ValueError(f"{path.name}: missing {', '.join(missing)}")
    for key in ("depends_on", "touches"):
        if not isinstance(fields[key], list):
            raise ValueError(f"{path.name}: {key} must be a [list]")
    task = Task(
        id=str(fields["id"]),
        title=str(fields["title"]),
        tool=str(fields["tool"]),
        priority=str(fields["priority"]),
        depends_on=tuple(fields["depends_on"]),
        touches=tuple(fields["touches"]),
        model=str(fields["model"]),
        effort=str(fields["effort"]),
        path=path,
    )
    problems = []
    if not ID_RE.match(task.id) or path.stem != task.id:
        problems.append(f"id {task.id!r} must look like AG-001 and match the file name")
    if task.priority not in PRIORITIES:
        problems.append(f"priority must be one of {PRIORITIES}")
    if task.model not in MODELS:
        problems.append(f"model must be one of {MODELS}")
    if task.effort not in EFFORTS:
        problems.append(f"effort must be one of {EFFORTS}")
    if not task.touches:
        problems.append("touches must name at least one path")
    if task.id in task.depends_on or any(not ID_RE.match(d) for d in task.depends_on):
        problems.append("depends_on must list other task IDs")
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    return task


def load_tasks(directory: Path = BACKLOG) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        task = load_task(path)
        tasks[task.id] = task
    return tasks


def _split(touch: str) -> tuple[str, str]:
    path, _, section = touch.partition("#")
    return path, section


def _paths_overlap(a: str, b: str) -> bool:
    if a == b:
        return True
    return (a.endswith("/") and b.startswith(a)) or (b.endswith("/") and a.startswith(b))


def touches_conflict(a: str, b: str) -> bool:
    """Two touches conflict unless they name different sections of one file."""
    path_a, section_a = _split(a)
    path_b, section_b = _split(b)
    if not _paths_overlap(path_a, path_b):
        return False
    return not (section_a and section_b and section_a != section_b)


def conflicts(a: Task, b: Task) -> bool:
    return any(touches_conflict(x, y) for x in a.touches for y in b.touches)


def ready(tasks: dict[str, Task]) -> list[Task]:
    """Open tasks whose dependencies are all done (their files deleted)."""
    return [t for t in tasks.values() if not any(d in tasks for d in t.depends_on)]


def plan(tasks: dict[str, Task], busy: set[str]) -> list[list[Task]]:
    """Group ready tasks into waves whose members touch disjoint files.

    Tasks already in progress (``busy``) are not scheduled, and nothing that
    conflicts with them is placed in the first wave.
    """
    locked = [tasks[i] for i in busy if i in tasks]
    pending = sorted(
        (t for t in ready(tasks) if t.id not in busy), key=lambda t: (t.priority, t.id)
    )
    waves: list[list[Task]] = []
    while pending:
        wave: list[Task] = []
        rest: list[Task] = []
        for task in pending:
            blockers = wave + (locked if not waves else [])
            (rest if any(conflicts(task, other) for other in blockers) else wave).append(task)
        if not wave:
            waves.append([])
            locked = []
            continue
        waves.append(wave)
        pending = rest
    return [w for w in waves if w]


def _row(task: Task) -> str:
    deps = ",".join(task.depends_on) or "-"
    tier = f"{task.model}/{task.effort}"
    return f"{task.id}  {task.priority}  {tier:<13}  deps={deps:<16} {task.title}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", nargs="?", default="list", choices=["list", "ready", "plan", "check"]
    )
    parser.add_argument("--busy", default="", help="comma-separated task IDs already in progress")
    parser.add_argument("--dir", type=Path, default=BACKLOG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks(args.dir)
    except ValueError as exc:
        print(f"backlog: {exc}", file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"{len(tasks)} task files are valid")
    elif args.command == "list":
        for task in sorted(tasks.values(), key=lambda t: t.id):
            print(_row(task))
    elif args.command == "ready":
        for task in sorted(ready(tasks), key=lambda t: (t.priority, t.id)):
            print(_row(task))
    else:
        busy = {i.strip() for i in args.busy.split(",") if i.strip()}
        for number, wave in enumerate(plan(tasks, busy), start=1):
            print(f"Wave {number}:")
            for task in wave:
                print("  " + _row(task))
    return 0


if __name__ == "__main__":
    sys.exit(main())
