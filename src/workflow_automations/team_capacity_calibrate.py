"""Measure the capacity model's bug-fix quota and estimate accuracy from ADO history.

The input is a saved ADO snapshot (``ado-graph`` or ``ado-triage``) of work in
past iterations, the roster and the ``team-capacity`` config with an
``iterations`` sheet naming those iterations. For each AreaPath and iteration the
tool compares completed work, split into bug fixing and other work, with the
hours the model predicted, and suggests ``rates`` rows to paste into the config.
Nothing contacts ADO and nothing changes the config.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from workflow_automations import team_capacity as tc
from workflow_automations.ado_common.excel import publish_all
from workflow_automations.ado_common.snapshot import RawSnapshot, SnapshotError, load_snapshot

logger = logging.getLogger("team_capacity_calibrate")

FIELD_TYPE = "System.WorkItemType"
FIELD_AREA = "System.AreaPath"
FIELD_ITERATION = "System.IterationPath"
FIELD_ASSIGNED_TO = "System.AssignedTo"
FIELD_COMPLETED = "Microsoft.VSTS.Scheduling.CompletedWork"
PARENT_REL = "System.LinkTypes.Hierarchy-Reverse"
DEFAULT_BUG_TYPES = ("Bug",)

CALIBRATION_COLUMNS = [
    "area_path",
    "iteration",
    "phase",
    "start",
    "end",
    "status",
    "people",
    "predicted_base",
    "predicted_bug_fix",
    "predicted_net",
    "bug_hours",
    "other_hours",
    "logged_hours",
    "configured_bug_fix_quota",
    "observed_bug_share",
    "suggested_bug_fix_quota",
    "estimate_accuracy",
]
RATES_COLUMNS = ["area_path", "from", "to", "phase", "bug_fix_quota", "focus_loss", "note"]

_EMAIL_IN_TEXT = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")
_ID_AT_END = re.compile(r"/workitems/(\d+)$", re.IGNORECASE)


@dataclass
class Logged:
    bug: float = 0.0
    other: float = 0.0

    @property
    def total(self) -> float:
        return self.bug + self.other


def assignee_email(value: Any) -> str:
    """The email in ``System.AssignedTo``: an identity object or ``Name <email>`` text."""
    if isinstance(value, dict):
        # On-premise servers often give a DOMAIN\\user uniqueName and the email elsewhere.
        candidates = [value.get(key) for key in ("uniqueName", "mailAddress")]
        value = next((c for c in candidates if c and "@" in str(c)), "")
    text = str(value or "").strip()
    match = _EMAIL_IN_TEXT.search(text)
    return (match.group(1) if match else text).lower() if "@" in text else ""


def parent_id(item: dict[str, Any]) -> int | None:
    for relation in item.get("relations") or ():
        if relation.get("rel") == PARENT_REL and relation.get("url"):
            match = _ID_AT_END.search(relation["url"])
            if match:
                return int(match.group(1))
    return None


def match_iteration(path: Any, iterations: list[tc.Iteration]) -> tc.Iteration | None:
    """The configured iteration whose name is the IterationPath or its last segment."""
    text = str(path or "").strip().lower()
    if not text:
        return None
    last = text.split("\\")[-1]
    for iteration in iterations:
        name = iteration.name.strip().lower()
        if name in (text, last):
            return iteration
    return None


def deepest_area(area_path: Any, areas: dict[tuple[str, ...], str]) -> str | None:
    parts = tc.area_parts(str(area_path or ""))
    for depth in range(len(parts), 0, -1):
        if parts[:depth] in areas:
            return areas[parts[:depth]]
    return None


def observed_work(
    snapshot: RawSnapshot,
    iterations: list[tc.Iteration],
    areas: dict[tuple[str, ...], str],
    pool_emails: set[str],
    bug_types: set[str],
) -> tuple[dict[tuple[str, str], Logged], list[tc.Problem]]:
    """Completed hours of the triage pool per (AreaPath, iteration), bugs apart.

    An item is bug work when it is a bug or its parent is (a task under a bug).
    """
    types = {wi_id: str(fields.get(FIELD_TYPE, "")) for wi_id, fields in snapshot.linked.items()}
    types.update({item["id"]: str(item["fields"].get(FIELD_TYPE, "")) for item in snapshot.items})
    logged: dict[tuple[str, str], Logged] = defaultdict(Logged)
    skipped: dict[str, float] = defaultdict(float)
    for item in snapshot.items:
        fields = item["fields"]
        try:
            hours = float(fields.get(FIELD_COMPLETED) or 0)
        except (TypeError, ValueError):
            hours = 0.0
        if hours <= 0:
            continue
        iteration = match_iteration(fields.get(FIELD_ITERATION), iterations)
        if iteration is None:
            skipped["iteration not in the config"] += hours
            continue
        area = deepest_area(fields.get(FIELD_AREA), areas)
        if area is None:
            skipped["AreaPath outside the roster's triage pool"] += hours
            continue
        if assignee_email(fields.get(FIELD_ASSIGNED_TO)) not in pool_emails:
            skipped["assigned outside the triage pool or unassigned"] += hours
            continue
        parent = parent_id(item)
        is_bug = types.get(item["id"], "").lower() in bug_types or (
            parent is not None and types.get(parent, "").lower() in bug_types
        )
        entry = logged[(area, iteration.name)]
        if is_bug:
            entry.bug += hours
        else:
            entry.other += hours
    problems = [
        tc.Problem("", "", "completed work not counted", f"{hours:g} hours: {reason}")
        for reason, hours in sorted(skipped.items())
    ]
    return logged, problems


def calibrate(
    snapshot: RawSnapshot,
    roster: pd.DataFrame,
    config: tc.Config,
    bug_types: set[str],
) -> dict[str, pd.DataFrame]:
    if not config.iterations:
        raise tc.CapacityError("Calibration needs an iterations sheet naming the past iterations")
    seen = {
        match_iteration(item["fields"].get(FIELD_ITERATION), config.iterations)
        for item in snapshot.items
    }
    named = [it for it in config.iterations if it in seen]
    if not named:
        raise tc.CapacityError("No snapshot item is in an iteration of the config")
    # Every configured iteration in the snapshot's span is reported, with or without work.
    iterations = [it for it in config.iterations if named[0].start <= it.start <= named[-1].start]
    first = min(it.start for it in iterations)
    last = max(it.end for it in iterations)
    result = tc.compute(roster, config, first, last)
    pool = [p for p in result.people if p.group.triage_pool]
    areas = {tc.area_parts(p.area_path): p.area_path for p in pool}
    logged, problems = observed_work(
        snapshot, iterations, areas, {p.email for p in pool}, bug_types
    )

    days = result.days[result.days["group"].isin({p.group.name for p in pool})]
    predicted = days.groupby(["area_path", "iteration"])[["bug_fix", "focus", "net"]].sum()
    people = days[days["gross"] > 0].groupby(["area_path", "iteration"])["email"].nunique()

    rows = []
    for area in sorted(set(areas.values()), key=str.lower):
        for iteration in iterations:
            key = (area, iteration.name)
            base = bug = net = 0.0
            if key in predicted.index:
                bug, focus, net = predicted.loc[key, ["bug_fix", "focus", "net"]]
                base = bug + focus + net
            work = logged.get(key, Logged())
            focus_loss = config.rate(area, iteration.start, "focus_loss")
            row: dict[str, Any] = {
                "area_path": area,
                "iteration": iteration.name,
                "phase": iteration.phase,
                "start": iteration.start,
                "end": iteration.end,
                "people": int(people.get(key, 0)),
                "predicted_base": base,
                "predicted_bug_fix": bug,
                "predicted_net": net,
                "bug_hours": work.bug,
                "other_hours": work.other,
                "logged_hours": work.total,
                "configured_bug_fix_quota": bug / base if base else None,
                "observed_bug_share": None,
                "suggested_bug_fix_quota": None,
                "estimate_accuracy": None,
                "focus_loss": focus_loss,
            }
            if work.total <= 0:
                row["status"] = "no logged work"
                problems.append(
                    tc.Problem("", area, "no logged work", f"iteration {iteration.name}")
                )
            elif base <= 0:
                row["status"] = "no predicted capacity"
            else:
                share = work.bug / work.total
                row.update(
                    status="ok",
                    observed_bug_share=share,
                    # Logged time is the base without its focus loss, which nobody logs.
                    suggested_bug_fix_quota=share * (1 - focus_loss),
                    estimate_accuracy=work.other / net if net else None,
                )
            rows.append(row)
    calibration = pd.DataFrame(rows, columns=[*CALIBRATION_COLUMNS, "focus_loss"])
    suggested = suggested_rates(calibration)
    calibration = calibration[CALIBRATION_COLUMNS]
    problem_frame = pd.DataFrame(
        [vars(p) for p in [*result.problems, *problems]],
        columns=["email", "area_path", "problem", "detail"],
    )
    return {"calibration": calibration, "suggested_rates": suggested, "problems": problem_frame}


def suggested_rates(calibration: pd.DataFrame) -> pd.DataFrame:
    """One ``rates`` row per AreaPath and run of consecutive iterations of one phase,
    weighted by logged hours; iterations without logged work are left out."""
    rows = []
    usable = calibration[calibration["status"] == "ok"]
    for area, frame in usable.groupby("area_path", sort=False):
        frame = frame.sort_values("start")
        run: list[pd.Series] = []
        for _, row in frame.iterrows():
            if run and row["phase"] != run[-1]["phase"]:
                rows.append(_rate_row(area, run))
                run = []
            run.append(row)
        if run:
            rows.append(_rate_row(area, run))
    return pd.DataFrame(rows, columns=RATES_COLUMNS)


def _rate_row(area: str, run: list[pd.Series]) -> dict[str, Any]:
    logged = sum(r["logged_hours"] for r in run)
    quota = sum(r["bug_hours"] * (1 - r["focus_loss"]) for r in run) / logged
    return {
        "area_path": area,
        "from": run[0]["start"],
        "to": run[-1]["end"],
        "phase": run[0]["phase"],
        "bug_fix_quota": round(quota, 3),
        "focus_loss": None,
        "note": f"{len(run)} iterations, {logged:g} logged hours",
    }


def run(args: argparse.Namespace) -> None:
    inputs = {path.resolve() for path in (args.snapshot, args.roster, args.config)}
    if args.output.resolve() in inputs:
        raise tc.CapacityError("--output may not be an input file")
    if args.output.exists() and not args.overwrite:
        raise tc.CapacityError(f"{args.output} exists; pass --overwrite to replace it")
    try:
        snapshot = load_snapshot(args.snapshot)
    except (OSError, SnapshotError) as exc:
        raise tc.CapacityError(f"Cannot read snapshot {args.snapshot}: {exc}") from exc
    roster_book = tc.read_book(args.roster, "roster")
    roster = roster_book[tc.find_roster_sheet(roster_book)]
    config = tc.read_config(tc.read_book(args.config, "config"), tc.roster_emails(roster))
    bug_types = {t.strip().lower() for t in args.bug_types.split(",") if t.strip()}
    outputs = calibrate(snapshot, roster, config, bug_types)
    publish_all(
        [
            (
                args.output,
                lambda tmp: tc.write_book({k: tc.excel_dates(v) for k, v in outputs.items()}, tmp),
            )
        ],
        overwrite=args.overwrite,
        exists_error=tc.CapacityError,
        exclusive=True,
    )
    logger.info(
        "Wrote %s: %d rows, %d suggested rates rows",
        args.output,
        len(outputs["calibration"]),
        len(outputs["suggested_rates"]),
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root.add_argument("--snapshot", type=Path, required=True, help="saved ADO snapshot (JSON)")
    root.add_argument("--roster", type=Path, required=True, help="team-roster output")
    root.add_argument(
        "--config", type=Path, required=True, help="team-capacity config with an iterations sheet"
    )
    root.add_argument("--output", type=Path, required=True, help="output workbook")
    root.add_argument(
        "--bug-types",
        default=",".join(DEFAULT_BUG_TYPES),
        help="comma-separated work item types that are bugs (default Bug)",
    )
    root.add_argument("--overwrite", action="store_true", help="replace an existing output")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stderr)
    logger.setLevel(logging.INFO)
    try:
        run(args)
    except tc.CapacityError as exc:
        logger.error("%s", exc)
        return 1
    except OSError as exc:
        logger.error("Cannot write output: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
