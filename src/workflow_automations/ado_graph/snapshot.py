"""Raw snapshots for ado-graph: capture fetched work items and replay them offline.

``snapshot_from_work_items`` records the SDK work items an export fetched, with
the filters and relation policy that shaped the walk. ``frames_from_snapshot``
rebuilds the node and edge tables the export would have written from them, and
``replay_export`` writes those files for ``analyze``, without contacting ADO.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from workflow_automations.ado_common.snapshot import RawSnapshot, SnapshotError, utc_timestamp
from workflow_automations.ado_graph.client import FIELD_RENAME
from workflow_automations.ado_graph.export import _parse_wi, _project_from_wi
from workflow_automations.ado_graph.report import _export_dataframes

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
    """
    if filtered_relations not in ("stop", "follow"):
        raise ValueError(
            f"filtered_relations must be 'stop' or 'follow', got {filtered_relations!r}"
        )
    return RawSnapshot(
        producer=PRODUCER,
        captured_at=captured_at if captured_at is not None else utc_timestamp(),
        source={"base_url": base_url, "project": project, "query_id": query_id},
        filters={
            "skip_types": sorted(t.lower() for t in skip_types),
            "include_projects": sorted(p.lower() for p in include_projects),
        },
        options={"filtered_relations": filtered_relations},
        roots=list(roots),
        omitted=sorted(set(omitted)),
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
    snapshot: RawSnapshot, output_dir: Path, formats: list[str], overwrite: bool = False
) -> None:
    """Write the export files of ``snapshot`` into ``output_dir`` for ``analyze``."""
    df_nodes, df_edges = frames_from_snapshot(snapshot)
    _export_dataframes(df_nodes, df_edges, output_dir, formats, overwrite=overwrite)


def _sdk_view(item: dict[str, Any]) -> SimpleNamespace:
    # _parse_wi reads SDK attributes; a raw item carries the same data as keys.
    relations = [SimpleNamespace(rel=r["rel"], url=r["url"]) for r in item["relations"]]
    return SimpleNamespace(id=item["id"], fields=item["fields"], relations=relations)
