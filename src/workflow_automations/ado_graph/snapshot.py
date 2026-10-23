"""Raw snapshots for ado-graph: capture fetched work items and replay them offline.

``snapshot_from_work_items`` records the SDK work items an export fetched, with
the filters and relation policy that shaped the walk. ``frames_from_snapshot``
rebuilds the node and edge tables the export would have written from them, and
``replay_export`` writes those files for ``analyze``, without contacting ADO.
``run_replay`` is the ``replay`` command around it.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from workflow_automations.ado_common.snapshot import (
    RawSnapshot,
    SnapshotError,
    load_snapshot,
    utc_timestamp,
)
from workflow_automations.ado_graph.client import FIELD_RENAME
from workflow_automations.ado_graph.export import EXIT_INCOMPLETE, _parse_wi, _project_from_wi
from workflow_automations.ado_graph.report import LongText, _export_dataframes

log = logging.getLogger("ado_tool")

PRODUCER = "ado-graph"
EDGE_COLUMNS = ["Source_ID", "Target_ID", "Link_Type"]


def raw_item(wi: Any) -> dict[str, Any]:
    """One SDK work item as plain data: ID, every field and every relation."""
    return {
        "id": wi.id,
        "fields": dict(wi.fields or {}),
        "relations": [{"rel": rel.rel or "", "url": rel.url} for rel in wi.relations or []],
    }


def snapshot_from_work_items(
    work_items: Iterable[Any],
    roots: Iterable[int],
    *,
    base_url: str,
    project: str,
    query_id: str,
    skip_types: Iterable[str] = (),
    include_projects: Iterable[str] = (),
    filtered_relations: str = "stop",
    omitted: Iterable[int] = (),
    captured_at: str | None = None,
) -> RawSnapshot:
    """Snapshot of an export run: every fetched work item in fetch order.

    Filters are the case-normalised values the export applied; out-of-scope items
    that were fetched are kept, so the replay applies the same filters to them.
    ``options`` also records provenance: the producing ``tool_version`` and the
    run's ``completeness`` (``complete``, or ``incomplete`` when linked IDs were
    omitted). Replay reads neither; it derives completeness from ``omitted``.
    """
    if filtered_relations not in ("stop", "follow"):
        raise ValueError(
            f"filtered_relations must be 'stop' or 'follow', got {filtered_relations!r}"
        )
    roots = list(roots)
    omitted = sorted(set(omitted))
    complete = set(omitted) <= set(roots)  # an omitted root fails the export instead
    return RawSnapshot(
        producer=PRODUCER,
        captured_at=captured_at if captured_at is not None else utc_timestamp(),
        source={"base_url": base_url, "project": project, "query_id": query_id},
        filters={
            "skip_types": sorted(t.lower() for t in skip_types),
            "include_projects": sorted(p.lower() for p in include_projects),
        },
        options={
            "filtered_relations": filtered_relations,
            "tool_version": tool_version(),
            "completeness": "complete" if complete else "incomplete",
        },
        roots=roots,
        omitted=omitted,
        items=[raw_item(wi) for wi in work_items if wi is not None],
    )


def frames_from_snapshot(snapshot: RawSnapshot) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Node and edge tables the export writes for the work items in ``snapshot``.

    Refuses snapshots that the export would not have written: those of another
    tool, which did not walk relations, and those missing a saved-query item.
    """
    if snapshot.producer != PRODUCER:
        raise SnapshotError(
            f"a {snapshot.producer} snapshot has no relation walk; ado-graph replays "
            f"only {PRODUCER} snapshots"
        )
    missing_roots = sorted(set(snapshot.roots) & set(snapshot.omitted))
    if missing_roots:
        raise SnapshotError(f"saved-query work items were not returned: {missing_roots}")
    mode = snapshot.options.get("filtered_relations", "stop")
    if mode not in ("stop", "follow"):
        raise SnapshotError(f"unknown filtered_relations option {mode!r}")
    skip_types = frozenset(snapshot.filters.get("skip_types", ()))
    include_projects = frozenset(snapshot.filters.get("include_projects", ()))

    node_rows: list[dict] = []
    edge_rows: list[dict] = []
    for item in snapshot.items:
        fields = item["fields"]
        wi_type = fields.get("System.WorkItemType", "Unknown")
        # The same scope rule as export: project first, then type.
        in_scope = not (
            (include_projects and _project_from_wi(fields).lower() not in include_projects)
            or (skip_types and wi_type.lower() in skip_types)
        )
        if not (in_scope or mode == "follow"):
            continue
        node_row, edges = _parse_wi(_sdk_view(item))
        node_row["In_Scope"] = in_scope
        node_rows.append(node_row)
        edge_rows.extend(edges)

    df_nodes = pd.DataFrame(node_rows).rename(columns=FIELD_RENAME)
    df_edges = pd.DataFrame(edge_rows, columns=EDGE_COLUMNS).drop_duplicates()
    return df_nodes, df_edges


def replay_export(
    snapshot: RawSnapshot,
    output_dir: Path,
    formats: list[str],
    overwrite: bool = False,
    long_text: LongText | None = None,
) -> list[int]:
    """Write the export files of ``snapshot`` into ``output_dir`` for ``analyze``.

    Returns the linked IDs ADO omitted, sorted: when there are any, the graph is
    incomplete, as the export's exit 4 reports.
    """
    df_nodes, df_edges = frames_from_snapshot(snapshot)
    _export_dataframes(
        df_nodes, df_edges, output_dir, formats, overwrite=overwrite, long_text=long_text
    )
    roots = set(snapshot.roots)
    omitted_linked = sorted(i for i in snapshot.omitted if i not in roots)
    if omitted_linked:
        log.warning(
            "%d linked work item(s) were not returned; the graph is incomplete: %s",
            len(omitted_linked),
            omitted_linked,
        )
    return omitted_linked


def run_replay(args: argparse.Namespace) -> None:
    """Entry point for `replay` mode: export files from a saved snapshot, offline.

    Exits 4 when the snapshot's linked IDs were omitted, as the export did.
    ``SnapshotError`` propagates for the caller to report.
    """
    snapshot = load_snapshot(Path(args.snapshot))
    log.info(
        "Replaying a snapshot captured %s by %s %s: %d work item(s), query %s.",
        snapshot.captured_at or "at an unknown time",
        snapshot.producer,
        snapshot.options.get("tool_version", "(version not recorded)"),
        len(snapshot.items),
        snapshot.source.get("query_id", "?"),
    )
    output_dir = Path(args.output_dir)
    formats = [f.strip().lower() for f in args.formats.split(",")]
    omitted_linked = replay_export(
        snapshot,
        output_dir,
        formats,
        overwrite=args.overwrite,
        long_text=LongText.from_args(args),
    )
    if omitted_linked:
        log.warning("Replay incomplete → %s (exit %d).", output_dir, EXIT_INCOMPLETE)
        raise SystemExit(EXIT_INCOMPLETE)
    log.info("Replay complete → %s", output_dir)


def tool_version() -> str:
    """Installed workflow-automations version, or ``unknown`` from a bare checkout."""
    try:
        return version("workflow-automations")
    except PackageNotFoundError:
        return "unknown"


def _sdk_view(item: dict[str, Any]) -> SimpleNamespace:
    # _parse_wi reads SDK attributes; a raw item carries the same data as keys.
    relations = [SimpleNamespace(rel=r["rel"], url=r["url"]) for r in item["relations"]]
    return SimpleNamespace(id=item["id"], fields=item["fields"], relations=relations)
