"""Command-line parser and `main` for the export and analyze modes."""

from __future__ import annotations

import argparse
import logging
import sys

from workflow_automations.ado_graph.analyze import (
    ALL_STEPS,
    DEFAULT_MAX_INDIRECT_EDGES,
    run_analyze,
)
from workflow_automations.ado_graph.client import ADO_BATCH_LIMIT, DEFAULT_SKIP_TYPES
from workflow_automations.ado_graph.export import run_export

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ado_tool")


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


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mode == "export":
        run_export(args)
    elif args.mode == "analyze":
        run_analyze(args)
    else:
        parser.print_help()
        sys.exit(1)
