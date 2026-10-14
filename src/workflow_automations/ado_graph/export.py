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

from workflow_automations.ado_common.relations import GRAPH
from workflow_automations.ado_common.snapshot import SnapshotError, save_snapshot, utc_timestamp
from workflow_automations.ado_graph.client import (
    ADO_BATCH_LIMIT,
    FIELD_RENAME,
    WIQL_FILTER_CHUNK,
    ADOClient,
    configure_ca_bundle,
)
from workflow_automations.ado_graph.report import _export_dataframes

log = logging.getLogger("ado_tool")

# Exit codes: 4 = outputs written but some linked IDs were omitted by ADO;
# 5 = seed IDs from the saved query were omitted, nothing written;
# 6 = outputs written but the --save-snapshot file could not be written.
EXIT_INCOMPLETE = 4
EXIT_SEEDS_MISSING = 5
EXIT_SNAPSHOT_FAILED = 6


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
      pruned           – queued IDs rejected by the WIQL filter, confirmed to
                         exist, and never fetched
                         (only with --filtered-relations stop).
      omitted          – IDs requested from ADO but not returned (deleted or
                         inaccessible); listed in omitted_ids and split into
                         seeds (from the saved query) and linked IDs.
      linked           – relation targets queued for the first time.
      fetched          – IDs sent in work-item batch requests.
      exported         – node rows written (None until the outputs are built).
      requests         – ADO calls: saved_query, wiql_filter, batch_fetch.

    Every ID is counted once, so the counters reconcile (see reconcile()):
      seeds + linked = pruned + fetched + queued
      fetched        = visited + omitted
      exported       = added (stop mode) or visited (follow mode)

    The completion status follows ado-triage's core-vs-label split, adapted
    to a graph: a missing seed means the scope itself is incomplete
    ("failed"); a missing linked ID loses a node and every path through it
    ("incomplete").  Pruned items are out of scope, not missing.
    """

    def __init__(self, seed_ids: frozenset[int] = frozenset(), follow: bool = False) -> None:
        self.added: collections.Counter = collections.Counter()
        self.skipped_type: collections.Counter = collections.Counter()
        self.excluded_project: collections.Counter = collections.Counter()
        self.pruned: int = 0
        self.omitted_ids: list[int] = []
        self.seed_ids = seed_ids
        self.follow = follow
        self.linked: int = 0
        self.fetched: int = 0
        self.exported: int | None = None
        self.requests: collections.Counter = collections.Counter()

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

    @property
    def omitted_seeds(self) -> list[int]:
        return sorted(i for i in self.omitted_ids if i in self.seed_ids)

    @property
    def omitted_linked(self) -> list[int]:
        return sorted(i for i in self.omitted_ids if i not in self.seed_ids)

    def reconcile(self, queue_remaining: int = 0) -> list[str]:
        """Return the counter identities that do not hold (empty when consistent)."""
        checks = [
            (
                "seeds + linked = pruned + fetched + queued",
                len(self.seed_ids) + self.linked,
                self.pruned + self.fetched + queue_remaining,
            ),
            (
                "fetched = visited + omitted",
                self.fetched,
                self.total_visited + len(self.omitted_ids),
            ),
        ]
        if self.exported is not None:
            expected = self.total_visited if self.follow else self.total_added
            checks.append(
                ("exported = " + ("visited" if self.follow else "added"), self.exported, expected)
            )
        return [f"{name} ({left:,} != {right:,})" for name, left, right in checks if left != right]

    @property
    def status(self) -> str:
        if self.omitted_seeds:
            return "failed"
        if self.omitted_linked:
            return "incomplete"
        return "complete"

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
        print(f"    seeds (from the saved query): {len(self.omitted_seeds):,}")
        print(f"    linked (reached via relations): {len(self.omitted_linked):,}")
        if queue_remaining:
            print(f"  Queue remaining: {queue_remaining:,}")
        print(
            f"  IDs: seeds {len(self.seed_ids):,} + linked {self.linked:,}"
            f" = pruned {self.pruned:,} + fetched {self.fetched:,} + queued {queue_remaining:,}"
        )
        exported = "-" if self.exported is None else f"{self.exported:,}"
        print(
            f"  Fetched {self.fetched:,} = visited {self.total_visited:,}"
            f" + omitted {len(self.omitted_ids):,}; exported nodes: {exported}"
        )
        print(
            f"  ADO requests: saved query {self.requests['saved_query']:,}"
            f" | WIQL filter calls {self.requests['wiql_filter']:,}"
            f" | work-item batches {self.requests['batch_fetch']:,}"
        )
        mismatches = self.reconcile(queue_remaining)
        print(f"  Counters reconcile: {'yes' if not mismatches else 'NO'}")
        print(f"  Completion status: {self.status}")
        print()
        for mismatch in mismatches:
            log.error("Export counters do not reconcile: %s", mismatch)

    def log_omitted(self) -> None:
        """Log the omitted IDs, sorted, so a rerun can be compared."""
        if self.omitted_seeds:
            log.error(
                "%d seed work item(s) from the saved query were not returned: %s",
                len(self.omitted_seeds),
                _preview_ids(self.omitted_seeds),
            )
        if self.omitted_linked:
            log.warning(
                "%d linked work item(s) were not returned; the graph is incomplete: %s",
                len(self.omitted_linked),
                _preview_ids(self.omitted_linked),
            )

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
        endpoint = GRAPH.endpoint(rel.rel, rel.url)
        if endpoint is None:
            continue  # unread link type, hyperlink, attachment, or other non-WI relation
        label, target_id = endpoint
        edge_rows.append({"Source_ID": wi_id, "Target_ID": target_id, "Link_Type": label})

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
    stats: ExportStats,
) -> int:
    """
    Drop queued IDs that fail the project/type filter, without fetching them.

    Runs only when the next fetch batch holds an ID not yet checked, and then
    checks up to one WIQL chunk of unchecked IDs from the queue head.  Every ID
    is sent to the WIQL filter at most once (rejected IDs to one more existence
    query).  Returns the number of IDs dropped.
    """
    if all(wi_id in wiql_checked for wi_id in itertools.islice(queued, batch_size)):
        return 0
    pending = list(
        itertools.islice((i for i in queued if i not in wiql_checked), WIQL_FILTER_CHUNK)
    )
    stats.requests["wiql_filter"] += 1
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
    # tqdm is in the ado-graph extra only; replay imports this module offline.
    from tqdm import tqdm

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
    captured_at = utc_timestamp()
    initial_ids = client.run_query_by_id(project, args.query_id)
    log.info("Query returned %d work item(s).", len(initial_ids))

    filtering = bool(include_projects or skip_types)
    follow_filtered = args.filtered_relations == "follow"
    stats = ExportStats(frozenset(initial_ids), follow=follow_filtered)
    stats.requests["saved_query"] += 1

    visited: set[int] = set()
    # IDs already sent to the WIQL filter, so each ID is filtered at most once.
    wiql_checked: set[int] = set()
    queued: dict[int, None] = dict.fromkeys(initial_ids)
    all_node_rows: list[dict] = []
    all_edge_rows: list[dict] = []
    # Every fetched SDK work item, in or out of scope, for --save-snapshot.
    fetched_items: list[Any] = []

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
                    stats,
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
            stats.requests["batch_fetch"] += 1
            stats.fetched += len(fetch_ids)
            raw_items = [wi for wi in client.get_work_items_batch(fetch_ids) if wi is not None]
            fetched_items.extend(raw_items)
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
                        if tid not in visited and tid not in queued:
                            queued[tid] = None
                            stats.linked += 1

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

            # Seeds head the FIFO queue, so they are all resolved before any
            # linked ID is fetched; fail as soon as that is the case.
            if stats.omitted_seeds and stats.seed_ids.isdisjoint(queued):
                break

    if stats.status == "failed":
        stats.print_summary(len(queued))
        stats.log_omitted()
        log.error("Export failed: the saved query's scope is incomplete; nothing written.")
        raise SystemExit(EXIT_SEEDS_MISSING)

    # 6. Build DataFrames
    df_nodes = pd.DataFrame(all_node_rows)
    df_nodes.rename(columns=FIELD_RENAME, inplace=True)

    df_edges = pd.DataFrame(
        all_edge_rows,
        columns=["Source_ID", "Target_ID", "Link_Type"],
    )
    df_edges.drop_duplicates(inplace=True)

    stats.exported = len(df_nodes)
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

    # 7. Snapshot first: it is the raw record even if writing the tables fails.
    # A failed snapshot must not lose the fetch, so the tables are still written.
    snapshot_saved = True
    if getattr(args, "save_snapshot", None):
        try:
            _save_snapshot(
                args, fetched_items, initial_ids, stats, skip_types, include_projects, captured_at
            )
        except (OSError, SnapshotError) as exc:
            snapshot_saved = False
            log.error("Snapshot not saved to %s: %s", args.save_snapshot, exc)

    # 8. Export
    formats = [f.strip().lower() for f in args.formats.split(",")]
    _export_dataframes(df_nodes, df_edges, output_dir, formats)
    stats.log_omitted()
    if not snapshot_saved:
        log.error(
            "Export written → %s, but not the snapshot (exit %d).",
            output_dir,
            EXIT_SNAPSHOT_FAILED,
        )
        raise SystemExit(EXIT_SNAPSHOT_FAILED)
    if stats.status == "incomplete":
        log.warning("Export incomplete → %s (exit %d).", output_dir, EXIT_INCOMPLETE)
        raise SystemExit(EXIT_INCOMPLETE)
    log.info("Export complete → %s", output_dir)


def _save_snapshot(
    args: argparse.Namespace,
    fetched_items: list[Any],
    initial_ids: list[int],
    stats: ExportStats,
    skip_types: frozenset[str],
    include_projects: frozenset[str],
    captured_at: str,
) -> None:
    from workflow_automations.ado_graph.snapshot import snapshot_from_work_items

    snapshot = snapshot_from_work_items(
        fetched_items,
        initial_ids,
        base_url=args.ado_url,
        project=args.project or "",
        query_id=args.query_id,
        skip_types=skip_types,
        include_projects=include_projects,
        filtered_relations=args.filtered_relations,
        omitted=stats.omitted_ids,
        captured_at=captured_at,
    )
    path = Path(args.save_snapshot)
    # Consent was checked when the run started, as for the export files.
    save_snapshot(snapshot, path, overwrite=True)
    log.info("Snapshot of %d fetched work item(s) → %s", len(snapshot.items), path)
