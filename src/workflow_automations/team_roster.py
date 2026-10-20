"""Classify corporate job titles, map people to ADO AreaPaths and count them per area.

The input is a staff export: one sheet with a ``corporatetitle`` column and the
columns used for mapping (``email``, ``department_name``, ``fullpath``). The output
is a copy of the workbook with ``classified_title`` and ``area_path`` added to that
sheet, a ``dev_per_area`` headcount pivot (AreaPath by classified title, approved
people only) and the three mapping sheets. Mapping is hierarchical: a person's own
row wins over their department, which wins over their organization path.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.utils import get_column_letter

from workflow_automations.ado_common.excel import excel_safe, keep_text_literal, publish_all

logger = logging.getLogger("team_roster")

# Main data sheet columns
CORPORATE_TITLE = "corporatetitle"
CLASSIFIED_TITLE = "classified_title"
FULLPATH = "fullpath"
DEPARTMENT_NAME = "department_name"
EMAIL = "email"
AREA_PATH = "area_path"
APPROVED = "approved"

# Mapping sheets and their columns
FULL_PATH = "full_path"
SHEET_PERSON_TO_AREA = "person_to_area"
SHEET_DEPARTMENT_TO_AREA = "department_to_area"
SHEET_FULLPATH_TO_AREA = "fullpath_to_area"
SHEET_DEV_PER_AREA = "dev_per_area"

# (substring of the lower-cased title, class); the first match wins, so more
# specific patterns come before general ones.
CLASSIFICATION_RULES: tuple[tuple[str, str], ...] = (
    ("lead developer", "lead developer"),
    ("research developer", "research developer"),
    ("testing engineer", "testing engineer"),
    ("test engineer", "testing engineer"),
    ("engineer in test", "sdet"),
    ("security analyst", "security analyst"),
    ("system analyst", "analyst"),
    ("head of", "head"),
    ("team lead", "team lead"),
    ("architect", "architect"),
    ("manager", "manager"),
    ("developer", "developer"),
)
UNKNOWN_TITLE = "Unknown"


@dataclass(frozen=True)
class Mapping:
    sheet: str
    source: str  # column in the main sheet
    key: str  # column in the mapping sheet
    counter: str


# Priority order: person -> department -> fullpath.
MAPPINGS: tuple[Mapping, ...] = (
    Mapping(SHEET_PERSON_TO_AREA, EMAIL, EMAIL, "person_matches"),
    Mapping(SHEET_DEPARTMENT_TO_AREA, DEPARTMENT_NAME, DEPARTMENT_NAME, "department_matches"),
    Mapping(SHEET_FULLPATH_TO_AREA, FULLPATH, FULL_PATH, "fullpath_matches"),
)

APPROVED_TEXT = frozenset({"TRUE", "True", "true", "1"})
MAX_COLUMN_WIDTH = 50


class RosterError(Exception):
    """Invalid input or an IO failure; the run exits 1 and writes nothing."""


@dataclass
class MatchStats:
    total: int = 0
    counts: dict[str, int] = field(
        default_factory=lambda: {m.counter: 0 for m in MAPPINGS} | {"no_matches": 0}
    )


# ---------------------------------------------------------------------------
# Classification and mapping
# ---------------------------------------------------------------------------


def classify_title(title: Any) -> str:
    """Class of ``title`` by case-insensitive substring match, else the title itself.

    Blank and non-text titles are ``Unknown``.
    """
    if not isinstance(title, str):
        return UNKNOWN_TITLE
    lowered = title.lower().strip()
    for pattern, category in CLASSIFICATION_RULES:
        if pattern in lowered:
            return category
    return title


def match_key(value: Any) -> str:
    """How a cell is compared with mapping keys: text, trimmed, lower-cased."""
    if _blank(value):
        return ""
    return str(value).strip().lower()


def _blank(value: Any) -> bool:
    return value is None or (not isinstance(value, str) and bool(pd.isna(value)))


def is_approved(value: Any) -> bool:
    """``approved`` as Excel and CSV exports spell it: TRUE, 1 or their text forms."""
    if isinstance(value, str):
        return value in APPROVED_TEXT
    try:
        return bool(value == 1)
    except (TypeError, ValueError):
        return False


def find_main_sheet(book: dict[str, pd.DataFrame]) -> str | None:
    """The first sheet with a ``corporatetitle`` column."""
    return next((name for name, df in book.items() if CORPORATE_TITLE in df.columns), None)


def add_classification(df: pd.DataFrame) -> bool:
    """Add ``classified_title`` unless the sheet already has it (then it is kept)."""
    if CLASSIFIED_TITLE in df.columns:
        logger.info("'%s' already exists; keeping it", CLASSIFIED_TITLE)
        return False
    df[CLASSIFIED_TITLE] = [classify_title(v) for v in df[CORPORATE_TITLE]]
    for category, count in df[CLASSIFIED_TITLE].value_counts().head(10).items():
        logger.info("  %s: %d", category, count)
    return True


def ensure_mapping_sheets(
    book: dict[str, pd.DataFrame], mapping_book: dict[str, pd.DataFrame]
) -> None:
    """Give ``book`` every mapping sheet with its key and ``area_path`` columns.

    A sheet already in the input wins over the mapping file's sheet of that name;
    a sheet in neither is created empty. Missing columns are added blank.
    """
    for mapping in MAPPINGS:
        required = [mapping.key, AREA_PATH]
        if mapping.sheet in book:
            sheet = book[mapping.sheet]
        elif mapping.sheet in mapping_book:
            sheet = book[mapping.sheet] = mapping_book[mapping.sheet]
        else:
            book[mapping.sheet] = pd.DataFrame(columns=required)
            continue
        for column in required:
            if column not in sheet.columns:
                logger.warning("Sheet '%s' has no '%s' column; adding it", mapping.sheet, column)
                sheet[column] = None


def build_lookup(sheet: pd.DataFrame, key: str) -> dict[str, Any]:
    """Mapping key -> AreaPath; rows with a blank key or AreaPath are ignored.

    A repeated key keeps its last row.
    """
    lookup: dict[str, Any] = {}
    for row in sheet.to_dict("records"):
        if not _blank(row.get(key)) and not _blank(row.get(AREA_PATH)):
            lookup[match_key(row[key])] = row[AREA_PATH]
    return lookup


def assign_area_paths(df: pd.DataFrame, book: dict[str, pd.DataFrame]) -> MatchStats | None:
    """Set ``area_path`` on every row from the first mapping that matches.

    Existing values are recomputed. Mappings whose source column is absent are
    skipped; with none present the column is left as it is and None is returned.
    """
    available = [m for m in MAPPINGS if m.source in df.columns]
    for mapping in MAPPINGS:
        if mapping not in available:
            logger.warning("Column '%s' not found; %s skipped", mapping.source, mapping.sheet)
    if not available:
        logger.error("No mapping column (%s) found", ", ".join(m.source for m in MAPPINGS))
        return None
    lookups = [(m, build_lookup(book[m.sheet], m.key)) for m in available]
    stats = MatchStats(total=len(df))
    paths = []
    for row in df.to_dict("records"):
        for mapping, lookup in lookups:
            key = match_key(row.get(mapping.source))
            if key and key in lookup:
                stats.counts[mapping.counter] += 1
                paths.append(lookup[key])
                break
        else:
            stats.counts["no_matches"] += 1
            paths.append(None)
    df[AREA_PATH] = pd.Series(paths, index=df.index, dtype=object)
    return stats


def log_stats(stats: MatchStats) -> None:
    def share(count: int) -> str:
        return f"{count} ({count / stats.total * 100 if stats.total else 0:.1f}%)"

    logger.info("Rows processed: %d", stats.total)
    for mapping in MAPPINGS:
        logger.info("%s matches: %s", mapping.sheet, share(stats.counts[mapping.counter]))
    logger.info("Total matched: %s", share(stats.total - stats.counts["no_matches"]))
    logger.info("No matches: %s", share(stats.counts["no_matches"]))


def validation_warnings(book: dict[str, pd.DataFrame]) -> list[str]:
    """One warning per sheet and column with blank ``area_path`` or ``classified_title``."""
    warnings = []
    for name, df in book.items():
        for column in (AREA_PATH, CLASSIFIED_TITLE):
            if column in df.columns and (blank := int(df[column].isna().sum())):
                warnings.append(f"Sheet '{name}': {blank} rows have empty {column}")
    return warnings


def headcount_pivot(df: pd.DataFrame) -> pd.DataFrame:
    """Approved people (non-blank email) per AreaPath and classified title.

    Rows without an AreaPath are not counted. Without an ``approved`` column every
    row counts.
    """
    if APPROVED in df.columns:
        df = df[[is_approved(v) for v in df[APPROVED]]]
    else:
        logger.warning("Column '%s' not found; the pivot counts every row", APPROVED)
    if df.empty or df[AREA_PATH].isna().all():
        logger.warning("No approved rows with an area_path; '%s' is empty", SHEET_DEV_PER_AREA)
        return pd.DataFrame()
    pivot = pd.pivot_table(
        df, values=EMAIL, index=AREA_PATH, columns=CLASSIFIED_TITLE, aggfunc="count", fill_value=0
    ).reset_index()
    pivot.columns.name = None
    return pivot


def sheet_order(book: dict[str, pd.DataFrame], main: str) -> list[str]:
    """Main sheet, pivot, mapping sheets in priority order, then the rest as read."""
    first = [main, SHEET_DEV_PER_AREA, *(m.sheet for m in MAPPINGS)]
    order = [name for name in dict.fromkeys(first) if name in book]
    return order + [name for name in book if name not in order]


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def read_book(path: Path, label: str) -> dict[str, pd.DataFrame]:
    try:
        book = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    except FileNotFoundError as exc:
        raise RosterError(f"{label} not found: {path}") from exc
    except Exception as exc:  # openpyxl and zipfile raise many types for a bad file
        raise RosterError(f"Cannot read {label} {path}: {exc}") from exc
    logger.info("Loaded %s %s: sheets %s", label, path, list(book))
    return book


def column_width(series: pd.Series, heading: Any) -> float:
    lengths = [len(str(v)) for v in series if not _blank(v)]
    return min(max([*lengths, len(str(heading))]) + 2, MAX_COLUMN_WIDTH)


def write_book(book: dict[str, pd.DataFrame], order: Sequence[str], path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name in order:
            df = excel_safe(book[name])
            df.to_excel(writer, sheet_name=name, index=False)
            worksheet = writer.sheets[name]
            keep_text_literal(worksheet)
            for position, heading in enumerate(df.columns, start=1):
                width = column_width(df.iloc[:, position - 1], heading)
                worksheet.column_dimensions[get_column_letter(position)].width = width
            logger.info("Saved sheet '%s' (%d rows, %d columns)", name, *df.shape)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def transform(
    book: dict[str, pd.DataFrame], mapping_book: dict[str, pd.DataFrame]
) -> tuple[str, list[str]]:
    """Run every step on ``book`` in place; return the main sheet and warnings."""
    main = find_main_sheet(book)
    if main is None:
        raise RosterError(f"No sheet has a '{CORPORATE_TITLE}' column")
    logger.info("Main data sheet: '%s'", main)
    df = book[main]
    add_classification(df)
    ensure_mapping_sheets(book, mapping_book)
    stats = assign_area_paths(df, book)
    if stats is not None:
        log_stats(stats)
    warnings = validation_warnings(book)
    for warning in warnings:
        logger.warning(warning)
    if AREA_PATH in df.columns and EMAIL in df.columns:
        book[SHEET_DEV_PER_AREA] = headcount_pivot(df)
    else:
        logger.error("Cannot create '%s': needs %s and %s", SHEET_DEV_PER_AREA, AREA_PATH, EMAIL)
    return main, warnings


def dry_run(book: dict[str, pd.DataFrame]) -> None:
    main = find_main_sheet(book)
    if main is None:
        raise RosterError(f"No sheet has a '{CORPORATE_TITLE}' column")
    df = book[main]
    logger.info("Main data sheet: '%s', %d rows, columns %s", main, len(df), list(df.columns))
    missing = [m.source for m in MAPPINGS if m.source not in df.columns]
    if missing:
        logger.warning("Missing mapping columns: %s", missing)
    logger.info("Sample title classifications:")
    for title in df[CORPORATE_TITLE].dropna().head(5):
        logger.info("  '%s' -> '%s'", title, classify_title(title))
    if APPROVED in df.columns:
        approved = sum(is_approved(v) for v in df[APPROVED])
        logger.info("Rows with %s=TRUE: %d/%d", APPROVED, approved, len(df))
    else:
        logger.warning("Column '%s' not found in data", APPROVED)


def check_output(args: argparse.Namespace) -> bool:
    """Whether to write ``args.output_file``; refuses to replace an input."""
    output = args.output_file.resolve()
    inputs = [args.input_file, *([args.mapping_file] if args.mapping_file else [])]
    if any(output == path.resolve() for path in inputs):
        raise RosterError(f"Output {args.output_file} would replace an input file")
    if not args.output_file.exists() or args.force:
        return True
    if not sys.stdin.isatty():
        raise RosterError(f"{args.output_file} exists; pass --force to overwrite it")
    answer = input(f"Output file '{args.output_file}' already exists. Overwrite? (y/n): ")
    if answer.lower() != "y":
        logger.info("Operation cancelled by user")
        return False
    return True


def run(args: argparse.Namespace) -> None:
    if not args.input_file.exists():
        raise RosterError(f"Input file does not exist: {args.input_file}")
    if args.mapping_file and not args.mapping_file.exists():
        raise RosterError(f"Mapping file does not exist: {args.mapping_file}")
    if args.dry_run:
        logger.info("DRY RUN: no output is written")
        dry_run(read_book(args.input_file, "input"))
        return
    if not check_output(args):
        return
    book = read_book(args.input_file, "input")
    mapping_book = read_book(args.mapping_file, "mapping file") if args.mapping_file else {}
    main, warnings = transform(book, mapping_book)
    try:
        publish_all(
            [(args.output_file, lambda tmp: write_book(book, sheet_order(book, main), tmp))],
            overwrite=True,
        )
    except OSError as exc:
        raise RosterError(f"Cannot write {args.output_file}: {exc}") from exc
    logger.info(
        "Saved %s%s", args.output_file, f" with {len(warnings)} warnings" if warnings else ""
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input.xlsx output.xlsx
  %(prog)s input.xlsx output.xlsx --mapping mapping_data.xlsx
  %(prog)s input.xlsx output.xlsx --verbose
  %(prog)s input.xlsx output.xlsx --dry-run
""",
    )
    root.add_argument("input_file", type=Path, help="Path to input Excel file")
    root.add_argument("output_file", type=Path, help="Path to output Excel file")
    root.add_argument(
        "-m",
        "--mapping",
        dest="mapping_file",
        type=Path,
        help="Excel file with person_to_area, department_to_area and fullpath_to_area sheets",
    )
    root.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging (wins over -q)")
    root.add_argument("-q", "--quiet", action="store_true", help="Warnings and errors only")
    root.add_argument("--dry-run", action="store_true", help="Analyze the input; write nothing")
    root.add_argument("--force", action="store_true", help="Overwrite an existing output file")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stderr)
    logger.setLevel(level)
    try:
        run(args)
    except RosterError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
