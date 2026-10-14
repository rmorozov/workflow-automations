"""`export` mode: BFS traversal over ADO relations and export statistics."""

from __future__ import annotations

import argparse
import collections
import itertools
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from workflow_automations.ado_graph.client import (
    ADO_BATCH_LIMIT,
    FIELD_RENAME,
    GRAPH_LINK_TYPES,
    LINK_LABEL,
    WIQL_FILTER_CHUNK,
    ADOClient,
    configure_ca_bundle,
)
from workflow_automations.ado_graph.report import _export_dataframes

log = logging.getLogger("ado_tool")


# ---------------------------------------------------------------------------
# Export statistics tracker
# ---------------------------------------------------------------------------


class ExportStats:
    """
    Tracks per-WorkItemType and per-project counts across the BFS export loop.

    Counters:
      added            – WIs accepted and stored as nodes, keyed by WorkItemType.
      skipped_type     – WIs excluded by the type filter, keyed by WorkItemType.
      excluded_project – WIs excluded by the project filter, keyed by project name.
      visited          = added + skipped_type + excluded_project (derived).
      pruned           – queued IDs rejected by the WIQL filter and never fetched
                         (only with --filtered-relations stop).
      omitted          – IDs requested from ADO but not returned (deleted or
                         inaccessible); listed in omitted_ids.
    """

    def __init__(self) -> None:
        self.added: collections.Counter = collections.Counter()
        self.skipped_type: collections.Counter = collections.Counter()
        self.excluded_project: collections.Counter = collections.Counter()
        self.pruned: int = 0
        self.omitted_ids: list[int] = []

    def record_added(self, wi_type: str) -> None:
        self.added[wi_type] += 1

    def record_skipped_type(self, wi_type: str) -> None:
        self.skipped_type[wi_type] += 1

    def record_excluded_project(self, project: str) -> None:
        self.excluded_project[project] += 1

    @property
    def total_added(self) -> int:
        return sum(self.added.values())

    @property
    def total_skipped_type(self) -> int:
        return sum(self.skipped_type.values())

    @property
    def total_excluded_project(self) -> int:
        return sum(self.excluded_project.values())

    @property
    def total_visited(self) -> int:
        return self.total_added + self.total_skipped_type + self.total_excluded_project

    def print_progress(self, queue_remaining: int) -> None:
        """Single-line progress update via logging (tqdm-friendly)."""
        log.info(
            "Progress – visited: %d | added: %d | skipped(type): %d"
            " | excluded(project): %d | pruned: %d | omitted: %d | queued: %d",
            self.total_visited,
            self.total_added,
            self.total_skipped_type,
            self.total_excluded_project,
            self.pruned,
            len(self.omitted_ids),
            queue_remaining,
        )

    def print_summary(self, queue_remaining: int = 0) -> None:
        """Print formatted per-type and per-project breakdown tables to stdout."""
        print()
        self._print_type_table()
        self._print_project_table()
        print(f"  Pruned via WIQL (not fetched): {self.pruned:,}")
        print(f"  Omitted by ADO (not returned): {len(self.omitted_ids):,}")
        if queue_remaining:
            print(f"  Queue remaining: {queue_remaining:,}")
        print()

    def _print_type_table(self) -> None:
        all_types: set[str] = set(self.added) | set(self.skipped_type)
        print("  ── By WorkItemType " + "─" * 42)
        if not all_types:
            print("  (no work items processed yet)")
            return
        col_w = max(max(len(t) for t in all_types), 12)
        header = f"  {'WorkItemType':<{col_w}}  {'Added':>8}  {'Skipped':>8}  {'Visited':>8}"
        sep = "  " + "-" * (col_w + 30)
        print(header)
        print(sep)
        for wi_type in sorted(all_types):
            a = self.added[wi_type]
            s = self.skipped_type[wi_type]
            print(f"  {wi_type:<{col_w}}  {a:>8}  {s:>8}  {a + s:>8}")
        print(sep)
        tv = self.total_added + self.total_skipped_type
        print(f"  {'TOTAL':<{col_w}}  {self.total_added:>8}  {self.total_skipped_type:>8}  {tv:>8}")
        print()

    def _print_project_table(self) -> None:
        print("  ── By Project (excluded) " + "─" * 36)
        if not self.excluded_project:
            print("  (no projects excluded)")
            return
        col_w = max(max(len(p) for p in self.excluded_project), 12)
        header = f"  {'Project':<{col_w}}  {'Excluded':>10}"
        sep = "  " + "-" * (col_w + 14)
        print(header)
        print(sep)
        for project in sorted(self.excluded_project):
            print(f"  {project:<{col_w}}  {self.excluded_project[project]:>10}")
        print(sep)
        print(f"  {'TOTAL':<{col_w}}  {self.total_excluded_project:>10}")
        print()


# ---------------------------------------------------------------------------
# Work-item parsing
# ---------------------------------------------------------------------------


def _parse_wi(wi: Any) -> tuple[dict, list[dict]]:
    """
    Convert a WorkItem SDK object into:
      node_row  : flat dict { WI_ID, <all field ref names> }
      edge_rows : list of { Source_ID, Target_ID, Link_Type }
    """
    wi_id: int = wi.id
    raw_fields = wi.fields or {}

    node_row: dict[str, Any] = {"WI_ID": wi_id}
    node_row.update(raw_fields)

    edge_rows: list[dict] = []
    for rel in wi.relations or []:
        rel_ref: str = rel.rel or ""
        if rel_ref not in GRAPH_LINK_TYPES:
            continue
        url_parts = (rel.url or "").rstrip("/").split("/")
        try:
            target_id = int(url_parts[-1])
        except (ValueError, IndexError):
            continue  # hyperlink, attachment, or other non-WI relation
        edge_rows.append(
            {
                "Source_ID": wi_id,
                "Target_ID": target_id,
                "Link_Type": LINK_LABEL.get(rel_ref, rel_ref),
            }
        )

    return node_row, edge_rows


# ---------------------------------------------------------------------------
# Export pipeline helpers
# ---------------------------------------------------------------------------


def _parse_skip_types(raw: str) -> frozenset[str]:
    """Parse --skip-types into a case-normalised frozenset."""
    if not raw.strip():
        return frozenset()
    return frozenset(t.strip().lower() for t in raw.split(",") if t.strip())


def _parse_include_projects(raw: str) -> frozenset[str]:
    """Parse --include-projects into a case-normalised frozenset."""
    if not raw.strip():
        return frozenset()
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _project_from_wi(wi_fields: dict) -> str:
    """
    Derive the ADO project name from a work item's field dict.
    Prefers System.TeamProject; falls back to first segment of System.AreaPath.
    """
    team_project: str = (wi_fields.get("System.TeamProject") or "").strip()
    if team_project:
        return team_project
    area_path: str = (wi_fields.get("System.AreaPath") or "").strip()
    return area_path.split("\\")[0] if area_path else "Unknown"


def _preview_ids(ids: list[int], limit: int = 20) -> str:
    shown = ", ".join(map(str, ids[:limit]))
    return shown + (f" … (+{len(ids) - limit} more)" if len(ids) > limit else "")


def _prune_queue_via_wiql(
    client: ADOClient,
    queued: dict[int, None],
    visited: set[int],
    wiql_checked: set[int],
    batch_size: int,
    include_projects: frozenset[str],
    skip_types: frozenset[str],
) -> int:
    """
    Drop queued IDs that fail the project/type filter, without fetching them.

    Runs only when the next fetch batch holds an ID not yet checked, and then
    checks up to one WIQL chunk of unchecked IDs from the queue head.  Every ID
    is sent to WIQL at most once.  Returns the number of IDs dropped.
    """
    if all(wi_id in wiql_checked for wi_id in itertools.islice(queued, batch_size)):
        return 0
    pending = list(
        itertools.islice((i for i in queued if i not in wiql_checked), WIQL_FILTER_CHUNK)
    )
    valid = client.filter_ids_via_wiql(pending, include_projects, skip_types)
    wiql_checked.update(pending)
    pruned = 0
    for wi_id in pending:
        if wi_id not in valid:
            del queued[wi_id]
            visited.add(wi_id)
            pruned += 1
    if pruned:
        log.info("Pruned %d out-of-scope queued WIs via WIQL.", pruned)
    return pruned


def run_export(args: argparse.Namespace) -> None:
    """Entry point for `export` mode."""

    # 1. Certificate setup
    cert_dir = getattr(args, "cert_dir", None) or os.environ.get("ADO_CERT_DIR")
    configure_ca_bundle(cert_dir)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 2. Resolve filters
    skip_types: frozenset[str] = _parse_skip_types(args.skip_types)
    include_projects: frozenset[str] = _parse_include_projects(
        getattr(args, "include_projects", "") or ""
    )

    if skip_types:
        log.info("Type filter active – skipping: %s", ", ".join(sorted(skip_types)))
    if include_projects:
        log.info("Project filter active – including only: %s", ", ".join(sorted(include_projects)))

    client = ADOClient(args.ado_url)
    project = args.project or ""
    batch_size = min(args.batch_size, ADO_BATCH_LIMIT)

    # 3. Initial IDs from stored query
    log.info("Running ADO query %s …", args.query_id)
    initial_ids = client.run_query_by_id(project, args.query_id)
    log.info("Query returned %d work item(s).", len(initial_ids))

    filtering = bool(include_projects or skip_types)
    follow_filtered = args.filtered_relations == "follow"

    visited: set[int] = set()
    # IDs already sent to the WIQL filter, so each ID is checked at most once.
    wiql_checked: set[int] = set()
    queued: dict[int, None] = dict.fromkeys(initial_ids)
    all_node_rows: list[dict] = []
    all_edge_rows: list[dict] = []
    stats = ExportStats()

    log.info(
        "Starting BFS export (batch_size=%d, filtered relations: %s).",
        batch_size,
        args.filtered_relations,
    )

    with tqdm(desc="Fetching work items", unit="WI") as pbar:
        while queued:
            # ── a) Classify unchecked queued IDs (stop mode only) ─────────
            # A WIQL filter cannot see relations, so dropping IDs before
            # fetch is only sound when relations of filtered items are not
            # followed anyway.
            if filtering and not follow_filtered:
                stats.pruned += _prune_queue_via_wiql(
                    client,
                    queued,
                    visited,
                    wiql_checked,
                    batch_size,
                    include_projects,
                    skip_types,
                )
                if not queued:
                    break

            # ── b) Pull next candidate batch ───────────────────────────────
            fetch_ids = []
            for wi_id in list(itertools.islice(queued, batch_size)):
                del queued[wi_id]
                if wi_id not in visited:
                    visited.add(wi_id)
                    fetch_ids.append(wi_id)

            if not fetch_ids:
                stats.print_progress(len(queued))
                continue

            # ── c) Batch fetch the IDs ─────────────────────────────────────
            raw_items = [wi for wi in client.get_work_items_batch(fetch_ids) if wi is not None]
            returned = {wi.id for wi in raw_items}
            missing = [wi_id for wi_id in fetch_ids if wi_id not in returned]
            if missing:
                stats.omitted_ids.extend(missing)
                log.warning(
                    "ADO omitted %d requested work item(s) (deleted or inaccessible): %s",
                    len(missing),
                    _preview_ids(missing),
                )

            # ── d) Process results ─────────────────────────────────────────
            for wi in raw_items:
                fields: dict = wi.fields or {}
                wi_type: str = fields.get("System.WorkItemType", "Unknown")
                wi_project: str = _project_from_wi(fields)
                node_row, edge_rows = _parse_wi(wi)

                # Filtering decides output scope; traversal is separate.
                if include_projects and wi_project.lower() not in include_projects:
                    stats.record_excluded_project(wi_project)
                    in_scope = False
                elif skip_types and wi_type.lower() in skip_types:
                    stats.record_skipped_type(wi_type)
                    in_scope = False
                else:
                    stats.record_added(wi_type)
                    in_scope = True

                if in_scope or follow_filtered:
                    for er in edge_rows:
                        tid: int = er["Target_ID"]
                        if tid not in visited:
                            queued.setdefault(tid, None)

                # In follow mode every fetched item is exported; out-of-scope
                # items are bridge nodes that keep accepted items connected in
                # `analyze`.  In stop mode an out-of-scope item is only fetched
                # when WIQL could not classify it, so it is not exported: the
                # output must not depend on whether the WIQL filter succeeded.
                if in_scope or follow_filtered:
                    node_row["In_Scope"] = in_scope
                    all_node_rows.append(node_row)
                    all_edge_rows.extend(edge_rows)

            pbar.update(len(fetch_ids))
            pbar.set_postfix(
                added=stats.total_added,
                skip_t=stats.total_skipped_type,
                excl_p=stats.total_excluded_project,
                queued=len(queued),
            )
            stats.print_progress(len(queued))

    # 6. Build DataFrames
    df_nodes = pd.DataFrame(all_node_rows)
    df_nodes.rename(columns=FIELD_RENAME, inplace=True)

    df_edges = pd.DataFrame(
        all_edge_rows,
        columns=["Source_ID", "Target_ID", "Link_Type"],
    )
    df_edges.drop_duplicates(inplace=True)

    stats.print_summary()
    log.info(
        "Collected %d nodes (%d in scope), %d edges  "
        "(skipped by type: %d | excluded by project: %d | pruned: %d | omitted: %d).",
        len(df_nodes),
        stats.total_added,
        len(df_edges),
        stats.total_skipped_type,
        stats.total_excluded_project,
        stats.pruned,
        len(stats.omitted_ids),
    )

    # 7. Export
    formats = [f.strip().lower() for f in args.formats.split(",")]
    _export_dataframes(df_nodes, df_edges, output_dir, formats)
    log.info("Export complete → %s", output_dir)
