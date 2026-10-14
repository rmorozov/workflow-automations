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
import posixpath
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

BACKLOG = Path(__file__).resolve().parent.parent / "docs" / "backlog"
ID_RE = re.compile(r"^[A-Z]{2}-\d{3}$")
REQUIRED = ("id", "title", "tool", "priority", "depends_on", "touches", "tier")
PRIORITIES = ("P1", "P2", "P3")
TIERS = ("mechanical", "routine", "complex", "deep")


@dataclass(frozen=True)
class Touch:
    """A declared edit: a path, a directory (``dir/``) or lines of one Markdown section."""

    path: str
    lines: range | None = None  # None means the whole file or directory


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    tool: str
    priority: str
    depends_on: tuple[str, ...]
    touches: tuple[str, ...]
    tier: str
    path: Path
    spans: tuple[Touch, ...] = ()


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


def slugify(heading: str) -> str:
    """GitHub-style heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def section_ranges(markdown: str) -> dict[str, range]:
    """Line range of each heading's section, including its subsections."""
    headings: list[tuple[int, int, str]] = []
    fenced = False
    lines = markdown.splitlines()
    for number, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
        match = None if fenced else re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if match:
            headings.append((number, len(match.group(1)), slugify(match.group(2))))
    ranges: dict[str, range] = {}
    for index, (start, level, slug) in enumerate(headings):
        end = next((n for n, lvl, _ in headings[index + 1 :] if lvl <= level), len(lines))
        ranges.setdefault(slug, range(start, end))
    return ranges


def canonical_path(raw: str) -> str:
    """Normalize a repository-relative path; a trailing ``/`` marks a directory."""
    path = posixpath.normpath(raw.replace("\\", "/"))
    if posixpath.isabs(path) or path == ".." or path.startswith("../"):
        raise ValueError(f"{raw}: must be a path inside the repository")
    return path + "/" if raw.endswith("/") and path != "." else path


def resolve_touch(touch: str, root: Path) -> Touch:
    """Map ``path#anchor`` to the anchor's line range when the file exists."""
    raw, _, anchor = touch.partition("#")
    path = canonical_path(raw)
    if not anchor:
        return Touch(path)
    file = root / path
    if not file.is_file():
        return Touch(path)  # a file to be created: reserve all of it
    ranges = section_ranges(file.read_text(encoding="utf-8"))
    if anchor not in ranges:
        raise ValueError(f"{touch}: no heading with anchor #{anchor}")
    return Touch(path, ranges[anchor])


def load_task(path: Path, root: Path | None = None) -> Task:
    root = root if root is not None else path.resolve().parents[2]
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
        tier=str(fields["tier"]),
        path=path,
    )
    problems = []
    if not ID_RE.match(task.id) or path.stem != task.id:
        problems.append(f"id {task.id!r} must look like AG-001 and match the file name")
    if task.priority not in PRIORITIES:
        problems.append(f"priority must be one of {PRIORITIES}")
    if task.tier not in TIERS:
        problems.append(f"tier must be one of {TIERS}")
    if not task.touches:
        problems.append("touches must name at least one path")
    if task.id in task.depends_on or any(not ID_RE.match(d) for d in task.depends_on):
        problems.append("depends_on must list other task IDs")
    spans: list[Touch] = []
    for touch in task.touches:
        try:
            spans.append(resolve_touch(touch, root))
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    return replace(task, spans=tuple(spans))


def find_cycle(tasks: dict[str, Task]) -> list[str] | None:
    """A dependency cycle among open tasks, as IDs from first to repeated, or None."""
    state: dict[str, int] = {}  # 1 visiting, 2 done
    stack: list[str] = []

    def visit(task_id: str) -> list[str] | None:
        state[task_id] = 1
        stack.append(task_id)
        for dep in sorted(tasks[task_id].depends_on):
            if dep not in tasks:  # deleted: done
                continue
            if state.get(dep) == 1:
                return stack[stack.index(dep) :] + [dep]
            if dep not in state and (cycle := visit(dep)):
                return cycle
        stack.pop()
        state[task_id] = 2
        return None

    for task_id in sorted(tasks):
        if task_id not in state and (cycle := visit(task_id)):
            return cycle
    return None


def load_tasks(directory: Path = BACKLOG) -> dict[str, Task]:
    root = directory.resolve().parents[1]
    tasks: dict[str, Task] = {}
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        task = load_task(path, root)
        tasks[task.id] = task
    cycle = find_cycle(tasks)
    if cycle:
        raise ValueError("dependency cycle: " + " -> ".join(cycle))
    return tasks


def _paths_overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


def touches_conflict(a: Touch, b: Touch) -> bool:
    """Touches conflict when their paths overlap, unless both name disjoint sections.

    A section includes its subsections, so a parent section conflicts with any
    section nested in it, while sibling sections do not.
    """
    if not _paths_overlap(a.path, b.path):
        return False
    if a.lines is None or b.lines is None:
        return True
    return a.lines.start < b.lines.stop and b.lines.start < a.lines.stop


def conflicts(a: Task, b: Task) -> bool:
    return any(touches_conflict(x, y) for x in a.spans for y in b.spans)


def ready(tasks: dict[str, Task]) -> list[Task]:
    """Open tasks whose dependencies are all done (their files deleted)."""
    return [t for t in tasks.values() if not any(d in tasks for d in t.depends_on)]


def waiting_on_busy(tasks: dict[str, Task], busy: set[str]) -> list[Task]:
    """Ready tasks that conflict with a task in progress; they wait for its PR."""
    locked = [tasks[i] for i in busy if i in tasks]
    return sorted(
        (
            t
            for t in ready(tasks)
            if t.id not in busy and any(conflicts(t, other) for other in locked)
        ),
        key=lambda t: (t.priority, t.id),
    )


def plan(tasks: dict[str, Task], busy: set[str]) -> list[list[Task]]:
    """Group ready tasks into waves whose members touch disjoint files.

    Tasks in progress (``busy``) and tasks that conflict with them are left out
    entirely (see ``waiting_on_busy``): they can be planned once the busy task
    is merged and its file deleted. Wave 1 can therefore always start now.
    """
    waiting = {t.id for t in waiting_on_busy(tasks, busy)}
    pending = sorted(
        (t for t in ready(tasks) if t.id not in busy and t.id not in waiting),
        key=lambda t: (t.priority, t.id),
    )
    waves: list[list[Task]] = []
    while pending:
        wave: list[Task] = []
        rest: list[Task] = []
        for task in pending:
            (rest if any(conflicts(task, other) for other in wave) else wave).append(task)
        waves.append(wave)
        pending = rest
    return waves


def _row(task: Task) -> str:
    deps = ",".join(task.depends_on) or "-"
    tier = task.tier
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
        waiting = waiting_on_busy(tasks, busy)
        if waiting:
            print("Waiting for in-progress tasks to merge:")
            for task in waiting:
                print("  " + _row(task))
    return 0


if __name__ == "__main__":
    sys.exit(main())
