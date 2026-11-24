"""cycle-time command line: spans, trace and report from normalized input files."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from workflow_automations.cycle_time import model, perfetto, report
from workflow_automations.cycle_time.spans import compute


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cycle-time",
        description="Split work-item cycle time into spans from ADO, Gerrit and CI data (offline).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("inputs", nargs="+", type=Path, help="cycle-time input JSON files")

    def output(p: argparse.ArgumentParser, required: bool) -> None:
        p.add_argument("-o", "--output", type=Path, required=required, help="output file")
        p.add_argument("--overwrite", action="store_true", help="replace an existing output file")

    spans = sub.add_parser("spans", help="write every span as CSV")
    common(spans)
    output(spans, required=False)
    trace = sub.add_parser("trace", help="write a Chrome JSON trace for the Perfetto UI")
    common(trace)
    output(trace, required=True)
    trace.add_argument("--mode", choices=perfetto.MODES, default="calendar")
    rep = sub.add_parser("report", help="p50/p95 hours per team and span kind")
    common(rep)
    output(rep, required=False)
    rep.add_argument("--csv", action="store_true", help="CSV instead of a Markdown table")
    return parser


def _write(text: str, path: Path | None, overwrite: bool) -> None:
    """Write through a uniquely named sibling temporary file.

    Without ``overwrite`` the file is published with a hard link, which fails if the
    target appeared meanwhile, so an existing file is never replaced. Nothing else in
    the directory is touched.
    """
    if path is None:
        sys.stdout.write(text)
        return
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists; pass --overwrite to replace it")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if overwrite:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError as exc:
                raise FileExistsError(f"{path} exists; pass --overwrite to replace it") from exc
    finally:
        tmp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        data = model.merge([model.read(p) for p in args.inputs])
        for warning in data.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        results = compute(data)
        if args.command == "spans":
            text = report.spans_csv(results)
        elif args.command == "trace":
            text = perfetto.dumps(results, args.mode)
        else:
            rows = report.summary(results)
            if args.csv:
                text = report.summary_csv(rows)
            else:
                text = report.summary_markdown(rows)
        _write(text, args.output, args.overwrite)
    except (model.InputError, FileExistsError, OSError) as exc:
        print(f"cycle-time: {exc}", file=sys.stderr)
        return 2
    return 0
