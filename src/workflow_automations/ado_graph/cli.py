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

from workflow_automations.ado_graph.analyze import (
    ALL_STEPS,
    DEFAULT_MAX_INDIRECT_EDGES,
    run_analyze,
)
from workflow_automations.ado_graph.client import (
    ADO_BATCH_LIMIT,
    DEFAULT_SKIP_TYPES,
    ca_bundle_scope,
    import_network_modules,
)
from workflow_automations.ado_graph.report import export_paths

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ado_tool",
        description="ADO Graph & Export CLI Tool",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # ── export ────────────────────────────────────────────────────────────
    ep = subparsers.add_parser("export", help="Fetch ADO work items and export data files.")
    ep.add_argument(
        "--ado-url",
        required=True,
        help="Base URL of the ADO instance, e.g. https://ado.corp.local/DefaultCollection",
    )
    ep.add_argument(
        "--query-id",
        required=True,
        help="GUID of the stored WIQL query to start from.",
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

    # ── replay ────────────────────────────────────────────────────────────
    rp = subparsers.add_parser(
        "replay", help="Write export data files from a saved snapshot, without ADO."
    )
    rp.add_argument("--snapshot", required=True, help="Snapshot saved by export --save-snapshot.")
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

    # ── analyze ───────────────────────────────────────────────────────────
    ap = subparsers.add_parser("analyze", help="Run graph analysis on exported parquet files.")
    ap.add_argument("--nodes-file", required=True, help="Path to nodes.parquet.")
    ap.add_argument("--edges-file", required=True, help="Path to edges.parquet.")
    ap.add_argument("--ado-url", default="", help="Base URL of the ADO instance.")
    ap.add_argument(
        "--effort-field",
        default="Microsoft.VSTS.Scheduling.OriginalEstimate",
        help="Full ADO reference name of the field used for sizing/estimates.",
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

    return parser


def _same_file(a: Path, b: Path) -> bool:
    return a.exists() and b.exists() and os.path.samefile(a, b)


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
    existing = [str(path) for path in outputs if path.exists()]
    if existing and not args.overwrite:
        parser.error(f"{', '.join(existing)} exists; pass --overwrite to replace it")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
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
        with ca_bundle_scope():
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
