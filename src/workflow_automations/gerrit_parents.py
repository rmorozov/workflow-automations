"""Match repositories listed in an XLSX column to their closest parent in a Gerrit tree.

The tree is a Markdown outline (nested bullets or headings) of Gerrit projects, such
as `All-Projects` with its child projects nested beneath it. Each repository is looked
up by its exact name, which may be a Gerrit path such as `platform/tools/repo`, and
otherwise by the path's last segment; its parent is the item directly above it.
Results go to a new workbook (the sheet plus Parent, Path and Status columns), a
Markdown outline that lists each parent once with its repositories, or both.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import Workbook

from workflow_automations.xlsx_outline import (
    ValidationError,
    escape,
    read_table,
    select_columns,
    write_output,
)
from workflow_automations.xlsx_unfold import (
    Item,
    literal,
    parse_outline,
    save,
    split_front_matter,
    unescape,
    validate_sheet_name,
)

MATCHED = "matched"
NOT_FOUND = "not found"
AMBIGUOUS = "ambiguous"
NO_PARENT = "no parent"
PATH_SEPARATOR = " / "
ADDED_COLUMNS = ["Parent", "Path", "Status"]


@dataclass
class Place:
    """Where a name occurs in the tree: its ancestors from the root, outermost first."""

    ancestors: tuple[str, ...]
    # The item's own name in the tree, which may differ from the repository looked up.
    name: str
    # First tree lines of this item and of its parent (0 at the top level).
    line: int
    parent_line: int
    leaf: bool

    @property
    def parent(self) -> str | None:
        return self.ancestors[-1] if self.ancestors else None


@dataclass
class Match:
    row: int
    repo: str
    status: str
    places: list[Place]

    @property
    def parent(self) -> str | None:
        return self.places[0].parent if self.status == MATCHED else None

    @property
    def path(self) -> tuple[str, ...]:
        return self.places[0].ancestors if self.status in {MATCHED, NO_PARENT} else ()


def index_tree(root: Item, raw: bool) -> dict[str, list[Place]]:
    """Map every item name to the distinct places it occurs, in document order."""
    places: dict[str, list[Place]] = {}
    first: dict[tuple[str, ...], int] = {(): 0}

    def walk(item: Item, ancestors: tuple[str, ...]) -> None:
        for child in item.children:
            name = unescape(child.text, raw)
            if not name:
                raise ValidationError(f"Tree line {child.line} has an empty item")
            line = first.setdefault((*ancestors, name), child.line)
            seen = places.setdefault(name, [])
            # The same name listed twice under the same parent is one project: it keeps
            # its first position and has children if any of its listings has them.
            same = [place for place in seen if place.ancestors == ancestors]
            if same:
                same[0].leaf &= not child.children
            else:
                seen.append(Place(ancestors, name, line, first[ancestors], not child.children))
            walk(child, (*ancestors, name))

    walk(root, ())
    return places


def read_tree(path: Path, raw: bool, title: bool) -> dict[str, list[Place]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    # Front matter (for example from xlsx-outline) carries no tree items.
    _, offset = split_front_matter(lines)
    places = index_tree(parse_outline(lines, offset, title), raw)
    if not places:
        raise ValidationError("Tree outline has no items")
    return places


def lookup(repo: str) -> str:
    """A sheet name as it is looked up: leading and trailing slashes ignored."""
    return repo.strip("/")


def segment(name: str) -> str:
    """The last segment of a Gerrit path: `repo` for `platform/tools/repo`."""
    return name.rsplit("/", 1)[-1]


def candidates(repo: str, places: dict[str, list[Place]]) -> list[Place]:
    """Places named exactly `repo`, else those that share its last path segment.

    A bare name matches any path ending in it, and a path matches a bare item named
    after its last segment. Two different full paths are different projects. Leading
    and trailing slashes are ignored, so `platform/tools/repo/` is that path.
    """
    repo = lookup(repo)
    if repo in places:
        return places[repo]
    if "/" in repo:
        return places.get(segment(repo), [])
    found = [
        place
        for name, listed in places.items()
        if "/" in name and segment(name) == repo
        for place in listed
    ]
    return sorted(found, key=lambda place: place.line)


def match(repos: list[tuple[int, str]], places: dict[str, list[Place]]) -> list[Match]:
    results = []
    for row, repo in repos:
        found = candidates(repo, places)
        if not found:
            status = NOT_FOUND
        elif len(found) > 1:
            status = AMBIGUOUS
        elif found[0].parent is None:
            status = NO_PARENT
        else:
            status = MATCHED
        results.append(Match(row, repo, status, found))
    return results


def render(results: list[Match], indent: int, raw: bool) -> str:
    """List each parent once with its repositories; unresolved names come last."""
    show = (lambda value: value) if raw else escape
    pad = " " * indent
    groups: dict[tuple[str, ...], dict[str, int]] = {}
    parent_lines: dict[tuple[str, ...], int] = {}
    for result in results:
        if result.status == MATCHED:
            place = result.places[0]
            groups.setdefault(result.path, {})[result.repo] = place.line
            parent_lines[result.path] = place.parent_line
    # Two parents with the same name in different branches are told apart by path.
    names = [path[-1] for path in groups]
    out: list[str] = []
    # Parents and their repositories follow the tree's order.
    for path in sorted(groups, key=parent_lines.__getitem__):
        label = path[-1] if names.count(path[-1]) == 1 else PATH_SEPARATOR.join(path)
        out.append(f"- {show(label)}")
        repos = groups[path]
        out.extend(f"{pad}- {show(repo)}" for repo in sorted(repos, key=repos.__getitem__))

    def section(label: str, status: str, nested: bool) -> None:
        seen: list[Match] = []
        for result in results:
            if result.status == status and all(result.repo != other.repo for other in seen):
                seen.append(result)
        if not seen:
            return
        out.append(f"- {label}")
        for result in seen:
            out.append(f"{pad}- {show(result.repo)}")
            if nested:
                out.extend(
                    f"{pad * 2}- {show(where(place, result.repo))}" for place in result.places
                )

    # Labels start with punctuation so they cannot be mistaken for a project name.
    section("(top level, no parent)", NO_PARENT, False)
    section("(under more than one parent)", AMBIGUOUS, True)
    section("(not found in tree)", NOT_FOUND, False)
    return "\n".join(out) + "\n" if out else ""


def where(place: Place, repo: str) -> str:
    """A candidate's path; one matched by last segment also names the tree item."""
    path = PATH_SEPARATOR.join(place.ancestors) or "(top level)"
    return path if place.name == lookup(repo) else f"{path} ({place.name})"


def workbook(sheet: str, headings: list[str], data, results: list[Match]) -> Workbook:
    validate_sheet_name(sheet)
    book = Workbook()
    ws = book.active
    ws.title = sheet
    by_row = {result.row: result for result in results}
    for column, value in enumerate([*headings, *ADDED_COLUMNS], 1):
        literal(ws, 1, column, value or None)
    for offset, values in enumerate(data):
        row = offset + 2
        result = by_row.get(row)
        added: list[str | None] = [None, None, None]
        if result is not None:
            if result.status == AMBIGUOUS:
                path = "; ".join(where(place, result.repo) for place in result.places)
            else:
                path = PATH_SEPARATOR.join(result.path) or None
            added = [result.parent, path, result.status]
        for column, value in enumerate([*values, *added], 1):
            literal(ws, row, column, value)
    return book


def run(args) -> tuple[str, list[Match]]:
    if args.indent < 1:
        raise ValidationError("--indent must be a positive integer")
    protected = [args.input, args.tree]
    for output, suffix in ((args.output, ".xlsx"), (args.outline, ".md")):
        if output is None:
            continue
        if any(output.resolve() == path.resolve() for path in protected):
            raise ValidationError("Outputs must be new files, not the workbook or tree")
        if output.suffix.lower() != suffix:
            raise ValidationError(f"{output} must be a {suffix} file")
        if output.exists() and not args.overwrite:
            raise ValidationError(f"Output exists; pass --overwrite to replace it: {output}")
    if args.output is not None and args.outline is not None:
        if args.output.resolve() == args.outline.resolve():
            raise ValidationError("--output and --outline must be different files")
    places = read_tree(args.tree, args.raw, args.title)
    sheet, headings, data = read_table(args.input, args.sheet)
    column = select_columns(headings, [args.column] if args.column else None, [1])[0]
    if args.output is not None and (clash := set(headings) & set(ADDED_COLUMNS)):
        raise ValidationError(f"Sheet already has columns named {', '.join(sorted(clash))}")
    repos = [(offset + 2, values[column]) for offset, values in enumerate(data) if values[column]]
    if not repos:
        raise ValidationError(f"Column {headings[column]} has no repository names")
    results = match(repos, places)
    content = render(results, args.indent, args.raw)
    if args.output is not None:
        save(workbook(args.output_sheet or sheet, headings, data, results), args.output)
    if args.outline is not None:
        write_output(args.outline, content, args.input, args.overwrite)
    return content, results


def summary(results: list[Match]) -> str:
    def names(status: str) -> list[str]:
        return list(dict.fromkeys(result.repo for result in results if result.status == status))

    matched = names(MATCHED)
    parents = {result.path for result in results if result.status == MATCHED}
    lines = [f"Matched {len(matched)} repositories to {len(parents)} parents"]
    for status, label in (
        (NO_PARENT, "Top level, no parent"),
        (AMBIGUOUS, "Under more than one parent"),
        (NOT_FOUND, "Not found in tree"),
    ):
        if found := names(status):
            lines.append(f"{label} ({len(found)}): {', '.join(found)}")
    by_segment = list(
        dict.fromkeys(
            f"{result.repo} ({result.places[0].name})"
            for result in results
            if result.status in {MATCHED, NO_PARENT}
            and result.places[0].name != lookup(result.repo)
        )
    )
    if by_segment:
        lines.append(f"Matched by last path segment ({len(by_segment)}): {', '.join(by_segment)}")
    inner = list(
        dict.fromkeys(
            result.repo
            for result in results
            if result.status == MATCHED and not result.places[0].leaf
        )
    )
    if inner:
        lines.append(
            f"Matched items that have children of their own ({len(inner)}): {', '.join(inner)}"
        )
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--input", type=Path, required=True, help="Workbook listing repositories")
    root.add_argument("--sheet", help="Sheet name (default: the first sheet)")
    root.add_argument("--column", help="Heading of the repository column (default: column A)")
    root.add_argument("--tree", type=Path, required=True, help="Markdown outline of the tree")
    root.add_argument(
        "--title", action="store_true", help="The tree's first heading is a document title"
    )
    root.add_argument("--output", type=Path, help="New .xlsx workbook with Parent, Path, Status")
    root.add_argument("--output-sheet", help="Sheet name in --output (default: the input's)")
    root.add_argument(
        "--outline",
        type=Path,
        help="Markdown file grouping repositories by parent (default without --output: "
        "standard output)",
    )
    root.add_argument("--overwrite", action="store_true", help="Replace existing outputs")
    root.add_argument("--indent", type=int, default=2, help="Spaces per nested bullet level")
    root.add_argument(
        "--raw", action="store_true", help="Do not unescape or escape Markdown characters"
    )
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        content, results = run(args)
    except (ValidationError, UnicodeError) as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 2
    except (OSError, BadZipFile) as exc:
        print(f"IO error: {exc}", file=sys.stderr)
        return 3
    if args.output is None and args.outline is None:
        sys.stdout.write(content)
    print(summary(results), file=sys.stderr)
    for path in (args.output, args.outline):
        if path is not None:
            print(f"Wrote {path}", file=sys.stderr)
    # Outputs are complete either way; 4 flags names that need a decision.
    return 0 if all(result.status == MATCHED for result in results) else 4


if __name__ == "__main__":
    raise SystemExit(main())
