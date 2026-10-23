"""Command-line parser and `main` for the export, replay and analyze modes.

Export's network modules load only when `export` runs, so `replay`, `analyze` and
`--help` need just the offline `ado-graph-analyze` extra.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from workflow_automations.ado_common import options
from workflow_automations.ado_graph.analyze import (
    ALL_STEPS,
    DEFAULT_CENTRALITY_SOURCES,
    DEFAULT_MAX_CYCLE_ROWS,
    DEFAULT_MAX_CYCLES,
    DEFAULT_MAX_INDIRECT_EDGES,
    DEFAULT_MAX_PATH_HOPS,
    META_PRESETS,
    MISSING_META_POLICIES,
    SUBGRAPHS,
    MetaFieldError,
    meta_field_errors,
    parse_meta_fields,
    run_analyze,
)
from workflow_automations.ado_graph.client import (
    ADO_BATCH_LIMIT,
    DEFAULT_SKIP_TYPES,
    import_network_modules,
)
from workflow_automations.ado_graph.report import (
    EDGE_COLUMNS,
    EXCEL_MAX_CELL_CHARS,
    EXPORT_FILES,
    LONG_TEXT_MODES,
    LONG_TEXT_SHEET,
    MIN_CELL_CHARS,
    NODE_KEY,
    export_paths,
)

log = logging.getLogger("ado_tool")


def configure_logging() -> None:
    """Root logging for command-line runs; importing the package leaves it alone."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _cell_chars(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {raw!r}") from None
    if not MIN_CELL_CHARS <= value <= EXCEL_MAX_CELL_CHARS:
        raise argparse.ArgumentTypeError(
            f"must be from {MIN_CELL_CHARS} to {EXCEL_MAX_CELL_CHARS:,}, got {value}"
        )
    return value


def add_long_text(parser: argparse.ArgumentParser, keep: str) -> None:
    """``--long-text`` and ``--max-cell-chars``; ``keep`` says what keep does in this mode."""
    parser.add_argument(
        "--long-text",
        choices=LONG_TEXT_MODES,
        default="split",
        help=(
            "Excel text longer than --max-cell-chars.  split (default): the cell keeps "
            "its start and a marker linking to a row of the "
            f"{LONG_TEXT_SHEET} sheet, which holds the full text split across Part_N "
            "cells.  trim: only the start and the marker.  keep: never change text; "
            f"{keep}"
        ),
    )
    parser.add_argument(
        "--max-cell-chars",
        type=_cell_chars,
        default=EXCEL_MAX_CELL_CHARS,
        metavar="N",
        help=(
            "Longest Excel cell text, marker included, for --long-text split and trim "
            f"({MIN_CELL_CHARS} to Excel's limit {EXCEL_MAX_CELL_CHARS:,}, the default)."
        ),
    )


KEEP_EXPORT = (
    "a cell over Excel's limit makes the run fail before writing; Parquet and CSV "
    "always keep the full text."
)


def build_parser() -> argparse.ArgumentParser:
    parser = options.new_parser(
        prog="ado_tool",
        description="ADO Graph & Export CLI Tool",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # ── export ────────────────────────────────────────────────────────────
    ep = subparsers.add_parser("export", help="Fetch ADO work items and export data files.")
    ep.add_argument(
        "--ado-url",
        help="Required.  Base URL of the ADO instance, e.g. https://ado.corp.local/DefaultCollection",
    )
    ep.add_argument(
        "--query-id",
        help="Required.  GUID of the stored WIQL query to start from.",
    )
    ep.add_argument(
        "--project",
        default="",
        help="ADO project name (needed for the WIQL endpoint on on-prem TFS/ADO).",
    )
    ep.add_argument(
        "--batch-size",
        type=_positive_int,
        default=200,
        help=f"WI fetch batch size, at least 1 (max {ADO_BATCH_LIMIT}, default 200).",
    )
    ep.add_argument(
        "--cert-dir",
        default=None,
        help=(
            "Directory of corporate CA certificates (.pem/.crt).  "
            "Merged with certifi bundle at startup.  Also: env var ADO_CERT_DIR."
        ),
    )
    ep.add_argument(
        "--output-dir",
        default="./output",
        help="Directory for exported files (default: ./output).",
    )
    ep.add_argument(
        "--formats",
        default="parquet,excel,csv",
        help="Comma-separated export formats: parquet, excel, csv.",
    )
    ep.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Replace existing export files in --output-dir and the --save-snapshot file "
            "(default: refuse before fetching)."
        ),
    )
    ep.add_argument(
        "--save-snapshot",
        default=None,
        metavar="PATH",
        help=(
            "Also save every fetched work item as a versioned JSON snapshot, which "
            "`replay` turns back into these export files without ADO."
        ),
    )
    ep.add_argument(
        "--skip-types",
        default=",".join(DEFAULT_SKIP_TYPES),
        metavar="TYPES",
        help=(
            "Comma-separated ADO WorkItemType values to mark out of scope "
            "(case-insensitive).  See --filtered-relations for traversal.  "
            "Default: %(default)s.  Pass '' to disable."
        ),
    )
    ep.add_argument(
        "--include-projects",
        default="",
        metavar="PROJECTS",
        help=(
            "Comma-separated ADO project names to keep in scope (case-insensitive).  "
            "Work items from other projects are marked out of scope.  "
            "Omit to accept all projects."
        ),
    )
    ep.add_argument(
        "--filtered-relations",
        choices=("follow", "stop"),
        default="stop",
        help=(
            "stop (default): do not traverse relations of filtered items; queued IDs "
            "are classified once via WIQL and filtered ones are not fetched (fewer ADO "
            "calls, smaller graph).  follow: fetch filtered work items, traverse their "
            "relations and export them as bridge nodes (In_Scope=False), so accepted "
            "items reachable only through them are found; excluded-project items are "
            "followed too, which can reach many unrelated projects."
        ),
    )
    add_long_text(ep, KEEP_EXPORT)

    # ── replay ────────────────────────────────────────────────────────────
    rp = subparsers.add_parser(
        "replay", help="Write export data files from a saved snapshot, without ADO."
    )
    rp.add_argument("--snapshot", help="Required.  Snapshot saved by export --save-snapshot.")
    rp.add_argument(
        "--output-dir",
        default="./output",
        help="Directory for exported files (default: ./output).",
    )
    rp.add_argument(
        "--formats",
        default="parquet,excel,csv",
        help="Comma-separated export formats: parquet, excel, csv.",
    )
    rp.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing export files in --output-dir (default: refuse).",
    )
    add_long_text(rp, KEEP_EXPORT)

    # ── analyze ───────────────────────────────────────────────────────────
    ap = subparsers.add_parser("analyze", help="Run graph analysis on exported parquet files.")
    ap.add_argument("--nodes-file", help="Required.  Path to nodes.parquet.")
    ap.add_argument("--edges-file", help="Required.  Path to edges.parquet.")
    ap.add_argument("--ado-url", default="", help="Base URL of the ADO instance.")
    ap.add_argument(
        "--effort-field",
        default="Microsoft.VSTS.Scheduling.OriginalEstimate",
        help="Full ADO reference name of the field used for sizing/estimates.",
    )
    ap.add_argument(
        "--meta-field",
        action="append",
        default=[],
        metavar="LABEL=FIELD",
        help=(
            "Add node column FIELD (for example Custom.Team) to every result sheet as "
            "LABEL; FIELD alone keeps its name.  Repeatable.  Default: none."
        ),
    )
    ap.add_argument(
        "--meta-preset",
        action="append",
        default=[],
        choices=tuple(META_PRESETS),
        help=(
            "Add a named set of metadata fields.  legacy: Roadmap_State=KL.Roadmap_State, "
            "the column every sheet carried before."
        ),
    )
    ap.add_argument(
        "--missing-meta",
        choices=MISSING_META_POLICIES,
        default="omit",
        help=(
            "A metadata field absent from the nodes file is logged and then omitted "
            "(default), written as a blank column (blank), or rejected with exit 2 (error)."
        ),
    )
    ap.add_argument(
        "--output-file",
        default="analysis_report.xlsx",
        help="Output Excel report path (default: analysis_report.xlsx).",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing --output-file (never one of the input files).",
    )
    ap.add_argument(
        "--steps",
        default="all",
        help=f"Comma-separated steps or 'all'.  Available: {', '.join(ALL_STEPS)}.",
    )
    ap.add_argument(
        "--bridges",
        choices=("contract", "drop"),
        default="contract",
        help=(
            "How to treat out-of-scope bridge nodes (In_Scope=False, from export "
            "--filtered-relations follow).  contract (default): replace each bridge with "
            "Indirect edges between its neighbours.  drop: remove bridges and their edges."
        ),
    )
    ap.add_argument(
        "--max-indirect-edges",
        type=_positive_int,
        default=DEFAULT_MAX_INDIRECT_EDGES,
        help=(
            "Budget of candidate indirect edges for bridge contraction (sum over bridges "
            "of in-degree × out-degree).  Exceeding it exits with an error before any "
            f"output is written.  Default: {DEFAULT_MAX_INDIRECT_EDGES:,}."
        ),
    )
    ap.add_argument(
        "--max-cycles",
        type=_positive_int,
        default=DEFAULT_MAX_CYCLES,
        help=(
            "cycles step: stop after this many cycles and mark the sheet "
            f"Cycles_Complete=False.  Default: {DEFAULT_MAX_CYCLES:,}."
        ),
    )
    ap.add_argument(
        "--max-cycle-rows",
        type=_positive_int,
        default=DEFAULT_MAX_CYCLE_ROWS,
        help=(
            "cycles step: stop after the cycle that brings its member rows to this many.  "
            f"Default: {DEFAULT_MAX_CYCLE_ROWS:,}."
        ),
    )
    ap.add_argument(
        "--centrality-sources",
        type=_positive_int,
        default=DEFAULT_CENTRALITY_SOURCES,
        help=(
            "metrics step: exact betweenness up to this many nodes, sampled from this "
            f"many sources (fixed seed) above it.  Default: {DEFAULT_CENTRALITY_SOURCES:,}."
        ),
    )
    ap.add_argument(
        "--max-path-hops",
        type=_positive_int,
        default=DEFAULT_MAX_PATH_HOPS,
        help=(
            "Path_Debug and CR_Path_Debug: show at most this many hops next to the row's "
            "item; a longer path names its far end and how many hops are not shown.  "
            f"Default: {DEFAULT_MAX_PATH_HOPS}."
        ),
    )
    add_long_text(
        ap,
        "a sheet with a cell over Excel's limit is written to a CSV file beside the "
        "report instead.",
    )

    for sub in (ep, rp, ap):
        options.add_options_file(sub)
    return parser


REQUIRED = {
    "export": ("--ado-url", "--query-id"),
    "replay": ("--snapshot",),
    "analyze": ("--nodes-file", "--edges-file"),
}


def _same_file(a: Path, b: Path) -> bool:
    return a.exists() and b.exists() and os.path.samefile(a, b)


def _comma_list(raw: str) -> list[str]:
    return [token.strip().lower() for token in raw.split(",") if token.strip()]


def _step_errors(raw: str) -> tuple[list[str], int]:
    """Return the --steps tokens `parse_steps` would skip, and how many it would run."""
    bad, steps = [], 0
    for token in raw.split(","):
        step, at, subgraph = (part.strip() for part in token.strip().lower().partition("@"))
        if not step and not at:
            continue
        if step == "all" and not subgraph:
            steps += len(ALL_STEPS)
        elif step not in ALL_STEPS or (subgraph and subgraph not in SUBGRAPHS):
            bad.append(token.strip())
        else:
            steps += 1
    return bad, steps


def _check_parquet(
    parser: argparse.ArgumentParser, option: str, name: str, required: tuple[str, ...]
) -> set[str]:
    """Read only the Parquet footer, require the columns analysis indexes, return all."""
    import pyarrow
    import pyarrow.parquet

    try:
        columns = set(pyarrow.parquet.read_schema(name).names)
    except (OSError, pyarrow.ArrowException) as exc:
        parser.error(f"{option} {name} is not a readable Parquet file ({exc})")
    missing = [col for col in required if col not in columns]
    if missing:
        parser.error(f"{option} {name} lacks required column(s) {', '.join(missing)}")
    return columns


def check_inputs(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject unknown formats or steps and inputs lacking required columns, before any work."""
    if args.mode in ("export", "replay"):
        formats = _comma_list(args.formats)
        unknown = [fmt for fmt in formats if fmt not in EXPORT_FILES]
        if unknown or not formats:
            given = f"unknown {', '.join(unknown)}" if unknown else "no format given"
            parser.error(f"--formats: {given}; choose from {', '.join(EXPORT_FILES)}")
        args.formats = ",".join(dict.fromkeys(formats))
    elif args.mode == "analyze":
        bad, steps = _step_errors(args.steps)
        if bad or not steps:
            given = f"unknown {', '.join(bad)}" if bad else "no step given"
            parser.error(
                f"--steps: {given}; choose 'all' or steps from {', '.join(ALL_STEPS)}, "
                f"optionally as step@subgraph with a subgraph from {', '.join(SUBGRAPHS)}"
            )
        try:
            meta_fields = parse_meta_fields(args.meta_field, args.meta_preset)
        except MetaFieldError as exc:
            parser.error(str(exc))
        columns = _check_parquet(parser, "--nodes-file", args.nodes_file, (NODE_KEY,))
        _check_parquet(parser, "--edges-file", args.edges_file, EDGE_COLUMNS)
        errors = meta_field_errors(meta_fields, columns, args.missing_meta)
        if errors:
            parser.error("--meta-field: " + "; ".join(errors))


def check_outputs(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse existing outputs without --overwrite and inputs as outputs, before any work."""
    if args.mode in ("export", "replay"):
        formats = [f.strip().lower() for f in args.formats.split(",")]
        outputs = export_paths(Path(args.output_dir), formats)
        if args.mode == "export" and args.save_snapshot:
            snapshot = Path(args.save_snapshot)
            if any(
                snapshot.resolve() == path.resolve() or _same_file(snapshot, path)
                for path in outputs
            ):
                parser.error(f"--save-snapshot {snapshot} is one of the export files")
            if snapshot.is_dir():
                parser.error(f"--save-snapshot {snapshot} is a directory")
            outputs.append(snapshot)
        if args.mode == "replay" and any(_same_file(Path(args.snapshot), o) for o in outputs):
            parser.error(f"--snapshot {args.snapshot} is one of the export files")
    else:
        output = Path(args.output_file)
        for option, name in (("--nodes-file", args.nodes_file), ("--edges-file", args.edges_file)):
            if _same_file(output, Path(name)):
                parser.error(f"--output-file {output} is the {option} input")
        outputs = [output]
    if args.options:
        options = Path(args.options)
        for path in outputs:
            if path.resolve() == options.resolve() or _same_file(path, options):
                parser.error(f"{path} is the --options input")
    existing = [str(path) for path in outputs if path.exists()]
    if existing and not args.overwrite:
        parser.error(f"{', '.join(existing)} exists; pass --overwrite to replace it")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args, command = options.parse(parser, argv)
    written = options.write_options(command, parser, args, f"ado-graph {args.mode} options")
    if written is not None:
        print(f"Wrote {written}")
        return
    options.require(parser, args, REQUIRED[args.mode])
    check_inputs(parser, args)
    check_outputs(parser, args)
    configure_logging()

    if args.mode == "export":
        try:
            import_network_modules()
            from workflow_automations.ado_graph.export import run_export
        except ModuleNotFoundError as exc:
            parser.error(
                "export needs the 'ado-graph' extra: "
                f"python -m pip install 'workflow-automations[ado-graph]' ({exc})"
            )
        run_export(args)
    elif args.mode == "replay":
        from workflow_automations.ado_common.snapshot import SnapshotError
        from workflow_automations.ado_graph.snapshot import run_replay

        try:
            run_replay(args)
        except (OSError, SnapshotError) as exc:
            parser.error(f"cannot replay {args.snapshot}: {exc}")
    elif args.mode == "analyze":
        run_analyze(args)
    else:
        parser.print_help()
        sys.exit(1)
