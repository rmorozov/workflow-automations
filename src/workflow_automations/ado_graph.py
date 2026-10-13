"""
ADO Graph & Export CLI Tool
============================
Two modes:
  export  – Fetches Work Items from an on-premise Azure DevOps instance via
             Kerberos auth (using the official azure-devops-python-api SDK)
             and exports nodes/edges to parquet, CSV, or Excel.
  analyze – Loads those files and runs NetworkX graph analysis, writing results
             to a multi-sheet Excel workbook.

Auth:
  Uses msrest.authentication.KerberosAuthentication (wraps requests-kerberos)
  injected into azure.devops.connection.Connection.

Certificates:
  A merged CA bundle is built at startup: certifi's default bundle is copied to
  a temp file, then every .pem/.crt file found under --cert-dir (or the env var
  ADO_CERT_DIR) is appended to it.  The merged file path is written to
  REQUESTS_CA_BUNDLE so that msrest / requests picks it up automatically.

Work-item filtering (export mode):
  --skip-types        comma-separated WorkItemType values to mark out of scope.
                      Default: Task, Bug, Test Case, Test Suite, Test Plan,
                      Test Run, Shared Steps, Shared Parameter.
                      Pass --skip-types "" to disable type filtering.
  --include-projects  comma-separated project names to keep in scope
                      (case-insensitive, System.TeamProject). Omit for all.

  Filtering decides output scope.  With the default --filtered-relations
  stop, relations of filtered items are not followed and queued IDs are
  classified once via WIQL so filtered items are usually not fetched at all.
  With --filtered-relations follow, every fetched work item's relations are
  traversed and filtered items are exported as bridge nodes
  (In_Scope=False), so `analyze` can keep accepted items connected through
  them; this can reach many unrelated projects.

Export statistics:
  After each BFS batch a live line is logged showing visited / added /
  skipped / excluded / pruned / omitted / queued counts.
  A formatted per-type and per-project table is printed when BFS completes.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import certifi
import networkx as nx
import pandas as pd

try:
    import openpyxl
    from openpyxl.utils import get_column_letter
except ImportError:
    openpyxl = None
from azure.devops.connection import Connection
from azure.devops.v7_0.work_item_tracking.models import (
    TeamContext,
    Wiql,
    WorkItemBatchGetRequest,
)
from msrest.authentication import KerberosAuthentication
from requests_kerberos import OPTIONAL
from tqdm import tqdm

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
# Constants
# ---------------------------------------------------------------------------

# Both forward and reverse relation refs so we recognise a relation regardless
# of which WI end returned it.
GRAPH_LINK_TYPES: set[str] = {
    "System.LinkTypes.Hierarchy-Forward",  # Parent → Child
    #    "System.LinkTypes.Hierarchy-Reverse",    # Child  → Parent
    "Microsoft.VSTS.Common.Affects-Forward",
    "Microsoft.VSTS.Common.Affects-Reverse",
    "Microsoft.VSTS.Common.Blocks-Forward",
    "Microsoft.VSTS.Common.Blocks-Reverse",
    "System.LinkTypes.Dependency-Forward",  # Predecessor → Successor
    "System.LinkTypes.Dependency-Reverse",
    "Microsoft.VSTS.Common.Implements-Forward",
    "Microsoft.VSTS.Common.Implements-Reverse",
}

LINK_LABEL: dict[str, str] = {
    "System.LinkTypes.Hierarchy-Forward": "Parent->Child",
    "System.LinkTypes.Hierarchy-Reverse": "Child->Parent",
    "Microsoft.VSTS.Common.Affects-Forward": "Affects",
    "Microsoft.VSTS.Common.Affects-Reverse": "Affected By",
    "Microsoft.VSTS.Common.Blocks-Forward": "Blocks",
    "Microsoft.VSTS.Common.Blocks-Reverse": "Blocked By",
    "System.LinkTypes.Dependency-Forward": "Predecessor->Successor",
    "System.LinkTypes.Dependency-Reverse": "Successor->Predecessor",
    "Microsoft.VSTS.Common.Implements-Forward": "Implements",
    "Microsoft.VSTS.Common.Implements-Reverse": "Implemented By",
}

# ADO batch endpoint hard limit
ADO_BATCH_LIMIT = 200

# ADO WIQL query_by_wiql hard limit (server-side top)
ADO_WIQL_PAGE = 20_000

# IDs per WIQL filter query; keeps the query text well under WIQL length limits
WIQL_FILTER_CHUNK = 1000

# Field renames: ADO reference name → friendly column name in DataFrames
FIELD_RENAME: dict[str, str] = {
    "System.Title": "Title",
    "System.WorkItemType": "WorkItemType",
    "System.State": "State",
}

# Default ADO WorkItemType values excluded from export (case-insensitive match).
# Users can override via --skip-types; pass "" to disable filtering entirely.
DEFAULT_SKIP_TYPES: tuple[str, ...] = (
    "Task",
    "Bug",
    "Test Case",
    "Test Suite",
    "Test Plan",
    "Test Run",
    "Shared Steps",
    "Shared Parameter",
)


# ---------------------------------------------------------------------------
# Certificate helpers
# ---------------------------------------------------------------------------


def build_merged_ca_bundle(cert_dir: str | None) -> str:
    """
    Create a temporary PEM file containing:
      1. All certificates from certifi's default bundle.
      2. Every *.pem / *.crt file found (recursively) under *cert_dir*.

    Returns the path to the merged temp file (lives for the process lifetime).
    """
    tmp = tempfile.NamedTemporaryFile(prefix="ado_ca_bundle_", suffix=".pem", delete=False)
    shutil.copyfile(certifi.where(), tmp.name)
    log.info("Merged CA bundle initialised from certifi (%s).", certifi.where())

    if cert_dir:
        cert_path = Path(cert_dir)
        if not cert_path.is_dir():
            log.warning("--cert-dir '%s' is not a directory – skipping custom certs.", cert_dir)
        else:
            added = 0
            for pattern in ("**/*.pem", "**/*.crt"):
                for cert_file in sorted(cert_path.glob(pattern)):
                    with open(tmp.name, "ab") as fh:
                        fh.write(b"\n")
                        fh.write(cert_file.read_bytes())
                    log.debug("  + appended cert: %s", cert_file)
                    added += 1
            log.info("Appended %d custom certificate file(s) from '%s'.", added, cert_dir)

    tmp.close()
    return tmp.name


def configure_ca_bundle(cert_dir: str | None) -> str:
    """Build the merged bundle, point REQUESTS_CA_BUNDLE at it, return path."""
    bundle_path = build_merged_ca_bundle(cert_dir)
    os.environ["REQUESTS_CA_BUNDLE"] = bundle_path
    log.info("REQUESTS_CA_BUNDLE → %s", bundle_path)
    return bundle_path


# ---------------------------------------------------------------------------
# ADOClient  (wraps azure-devops-python-api SDK)
# ---------------------------------------------------------------------------


class ADOClient:
    """
    Thin wrapper around the official azure-devops-python-api SDK.

    Authentication: KerberosAuthentication from msrest, which delegates to
    requests-kerberos, using the current Kerberos ticket cache.

    Certificate handling: done before constructing this object via
    configure_ca_bundle(); REQUESTS_CA_BUNDLE is already set and msrest
    respects it automatically.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        credentials = KerberosAuthentication(mutual_authentication=OPTIONAL)
        self._connection = Connection(base_url=self.base_url, creds=credentials)
        self._wit_client = None

    @property
    def wit(self):
        """Return (and lazily initialise) the WorkItemTrackingClient."""
        if self._wit_client is None:
            self._wit_client = self._connection.clients.get_work_item_tracking_client()
        return self._wit_client

    # ------------------------------------------------------------------
    # Stored-query execution
    # ------------------------------------------------------------------

    def run_query_by_id(self, project: str, query_id: str) -> list[int]:
        """
        Execute a stored WIQL query by its GUID and return flat WI ID list.

        Handles both flat-list results (.work_items) and tree/link results
        (.work_item_relations) transparently.
        """
        team_context = TeamContext(project=project) if project else None
        result = self.wit.query_by_id(id=query_id, team_context=team_context)

        ids: list[int] = []
        if result.work_items:
            ids = [ref.id for ref in result.work_items]
        elif result.work_item_relations:
            seen: set[int] = set()
            for link in result.work_item_relations:
                for ref in (link.source, link.target):
                    if ref and ref.id not in seen:
                        seen.add(ref.id)
                        ids.append(ref.id)
        return ids

    def filter_ids_via_wiql(
        self,
        ids: list[int],
        include_projects: frozenset[str],
        skip_types: frozenset[str],
    ) -> set[int]:
        """
        Takes a list of IDs and returns the subset that matches the project
        and type filters using bulk WIQL queries.
        """
        valid_ids = set()

        # Process in chunks of 1000 to avoid exceeding WIQL string length limits
        for chunk in _iter_chunks(ids, WIQL_FILTER_CHUNK):
            id_list = ",".join(map(str, chunk))
            wiql = f"SELECT [System.Id] FROM WorkItems WHERE [System.Id] IN ({id_list})"

            # System.TeamProject is the native, fully indexed way to filter projects
            # Note: WIQL escapes single quotes by doubling them ('')
            if include_projects:
                projs = ", ".join("'{}'".format(p.replace("'", "''")) for p in include_projects)
                wiql += f" AND [System.TeamProject] IN ({projs})"

            if skip_types:
                types = ", ".join("'{}'".format(t.replace("'", "''")) for t in skip_types)
                wiql += f" AND [System.WorkItemType] NOT IN ({types})"

            try:
                res = self.wit.query_by_wiql(wiql=Wiql(query=wiql), top=ADO_WIQL_PAGE)
                if res.work_items:
                    valid_ids.update(ref.id for ref in res.work_items)
            except Exception as e:
                log.warning("WIQL filter chunk failed (skipping filter for chunk): %s", e)
                # Fallback: assume all are valid if query fails, BFS will handle them natively
                valid_ids.update(chunk)

        return valid_ids

    # ------------------------------------------------------------------
    # Batch work-item fetch (fields + relations)
    # ------------------------------------------------------------------

    def get_work_items_batch(self, ids: list[int]) -> list[Any]:
        """
        Fetch up to ADO_BATCH_LIMIT work items in one SDK call with relations
        expanded.  Returns a list of WorkItem SDK objects.
        """
        if not ids:
            return []
        request = WorkItemBatchGetRequest(ids=ids, expand="Relations", error_policy="Omit")
        return self.wit.get_work_items_batch(request) or []

    def get_available_fields(self) -> list[str]:
        """Return all field reference names defined in the collection."""
        fields = self.wit.get_fields()
        return [f.reference_name for f in (fields or [])]


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


def _iter_chunks(lst: list, size: int) -> Iterator[list]:
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


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


def _export_dataframes(
    df_nodes: pd.DataFrame,
    df_edges: pd.DataFrame,
    output_dir: Path,
    formats: list[str],
) -> None:
    dispatch = {
        "parquet": _export_parquet,
        "csv": _export_csv,
        "excel": _export_excel,
    }
    for fmt in formats:
        fn = dispatch.get(fmt)
        if fn is None:
            log.warning("Unknown export format '%s' – skipping.", fmt)
            continue
        fn(df_nodes, df_edges, output_dir)
        log.info("Exported %s.", fmt)


def _export_parquet(df_nodes: pd.DataFrame, df_edges: pd.DataFrame, d: Path) -> None:
    df_nodes.to_parquet(d / "nodes.parquet", index=False)
    df_edges.to_parquet(d / "edges.parquet", index=False)


def _export_csv(df_nodes: pd.DataFrame, df_edges: pd.DataFrame, d: Path) -> None:
    df_nodes.to_csv(d / "nodes.csv", index=False)
    df_edges.to_csv(d / "edges.csv", index=False)


def _export_excel(df_nodes: pd.DataFrame, df_edges: pd.DataFrame, d: Path) -> None:
    with pd.ExcelWriter(d / "work_items.xlsx", engine="openpyxl") as writer:
        df_nodes.to_excel(writer, sheet_name="Nodes", index=False)
        df_edges.to_excel(writer, sheet_name="Edges", index=False)


# ---------------------------------------------------------------------------
# GraphAnalyzer
# ---------------------------------------------------------------------------


DEFAULT_MAX_INDIRECT_EDGES = 1_000_000


class ContractionBudgetExceeded(ValueError):
    """Bridge contraction would create more indirect edges than allowed."""


def _edge_path(edge_data: dict) -> tuple[str, ...]:
    return edge_data.get("path") or (str(edge_data.get("link_type")),)


def _edge_label(edge_data: dict) -> str:
    """Link label for reports; indirect edges name their bridges and link types."""
    label = str(edge_data.get("link_type"))
    via = edge_data.get("via")
    if not via:
        return label
    return f"{label} via {'>'.join(map(str, via))} ({', '.join(_edge_path(edge_data))})"


class GraphAnalyzer:
    """Builds a NetworkX DiGraph and runs the requested analysis steps."""

    # Only forward-direction labels so each logical relationship maps to
    # exactly ONE directed edge (avoids contradictory A→B and B→A pairs).
    DIRECTED_LINK_TYPES: set[str] = {
        "Parent->Child",
        "Affects",
        "Blocks",
        "Predecessor->Successor",
        "Implements",
    }

    # Map reverse link types to their forward counterparts and indicate that nodes must flip
    REVERSE_LINK_MAP: dict[str, str] = {
        "Child->Parent": "Parent->Child",
        "Affected By": "Affects",
        "Blocked By": "Blocks",
        "Successor->Predecessor": "Predecessor->Successor",
        "Implemented By": "Implements",
    }

    def __init__(
        self,
        df_nodes: pd.DataFrame,
        df_edges: pd.DataFrame,
        ado_url: str = "",
        effort_field: str = "",
        bridges: str = "contract",
        max_indirect_edges: int = DEFAULT_MAX_INDIRECT_EDGES,
    ) -> None:
        self.df_nodes = df_nodes
        self.df_edges = df_edges
        self.ado_url = ado_url.rstrip("/")
        self.effort_field = effort_field
        self.bridges = bridges
        self.max_indirect_edges = max_indirect_edges
        self.G: nx.DiGraph = nx.DiGraph()
        self._build_graph()

    def _wi_url(self, wi_id: int) -> str:
        return f"{self.ado_url}/_workitems/edit/{wi_id}" if self.ado_url else ""

    def _build_graph(self) -> None:
        valid_nodes = set(self.df_nodes["WI_ID"])

        for _, row in self.df_nodes.iterrows():
            self.G.add_node(
                row["WI_ID"],
                WorkItemType=row.get("WorkItemType", ""),
                Title=row.get("Title", ""),
                State=row.get("State", ""),
                Roadmap_State=row.get("KL.Roadmap_State", ""),
                AreaPath=row.get("System.AreaPath", ""),
                IterationPath=row.get("System.IterationPath", ""),
            )

        # Normalize and ingest edges
        edge_count = 0
        for _, row in self.df_edges.iterrows():
            src, tgt, ltype = row["Source_ID"], row["Target_ID"], row["Link_Type"]

            # Skip phantom nodes not present in nodes dataframe
            if src not in valid_nodes or tgt not in valid_nodes:
                continue

            if ltype in self.DIRECTED_LINK_TYPES:
                # Regular forward edge
                self.G.add_edge(src, tgt, link_type=ltype)
                edge_count += 1
            elif ltype in self.REVERSE_LINK_MAP:
                # FIX: Flip the edge direction and normalize the label
                normalized_type = self.REVERSE_LINK_MAP[ltype]
                self.G.add_edge(tgt, src, link_type=normalized_type)
                edge_count += 1

        log.info(
            "Graph built: %d nodes, %d normalized edges.", self.G.number_of_nodes(), edge_count
        )
        self._contract_bridges()

    def _contract_bridges(self) -> None:
        """
        Replace out-of-scope bridge nodes (In_Scope == False) with indirect edges.

        For a bridge B with P -> B -> S, add P -> S labelled "Indirect", with the
        bridge IDs in its `via` attribute and the normalized link types along the
        way in `path`, unless P -> S already exists.  Chains of
        bridges collapse into one edge.  A cycle passing only through bridges back
        to the same node (P -> B -> P) is dropped rather than kept as a self-loop.
        Exports without an In_Scope column are treated as fully in scope.

        Each bridge costs predecessors × successors candidate pairs.  Before a
        bridge is contracted, the running total is checked against
        max_indirect_edges and ContractionBudgetExceeded is raised, so a dense
        bridge fails fast instead of materializing millions of edges.  With
        bridges="drop", bridges are removed without adding indirect edges.
        """
        if "In_Scope" not in self.df_nodes.columns:
            return
        in_scope = self.df_nodes["In_Scope"].fillna(True).astype(bool)
        bridges = list(self.df_nodes.loc[~in_scope, "WI_ID"])
        added = 0
        work = 0
        for bridge in bridges:
            if self.bridges == "drop":
                self.G.remove_node(bridge)
                continue
            work += self.G.in_degree(bridge) * self.G.out_degree(bridge)
            if work > self.max_indirect_edges:
                raise ContractionBudgetExceeded(
                    f"bridge contraction needs more than {self.max_indirect_edges:,} candidate "
                    f"indirect edges (reached {work:,} at bridge {bridge}); rerun with a larger "
                    "--max-indirect-edges or with --bridges drop"
                )
            for pred in list(self.G.predecessors(bridge)):
                for succ in list(self.G.successors(bridge)):
                    if pred == succ or pred == bridge or succ == bridge:
                        continue
                    if self.G.has_edge(pred, succ):
                        continue
                    into, out = self.G[pred][bridge], self.G[bridge][succ]
                    via = into.get("via", ()) + (bridge,) + out.get("via", ())
                    path = _edge_path(into) + _edge_path(out)
                    self.G.add_edge(pred, succ, link_type="Indirect", via=via, path=path)
                    added += 1
            self.G.remove_node(bridge)
        self.df_nodes = self.df_nodes[in_scope].reset_index(drop=True)
        log.info(
            "%s %d bridge node(s); added %d indirect edge(s); %d in-scope nodes remain.",
            "Dropped" if self.bridges == "drop" else "Contracted",
            len(bridges),
            added,
            self.G.number_of_nodes(),
        )

    def _meta(self, wi_id: int, prefix: str = "") -> dict:
        """Helper: Returns standardized metadata columns for any Node."""
        d = self.G.nodes.get(wi_id, {})
        return {
            f"{prefix}Title": d.get("Title", ""),
            f"{prefix}WI_URL": self._wi_url(wi_id),
            f"{prefix}State": d.get("State", ""),
            f"{prefix}Roadmap_State": d.get("Roadmap_State", ""),
            f"{prefix}WorkItemType": d.get("WorkItemType", ""),
            f"{prefix}AreaPath": d.get("AreaPath", ""),
            f"{prefix}IterationPath": d.get("IterationPath", ""),
        }

    def _get_neighborhood_diagnostic(self, node_id: int) -> str:
        """Helper to generate a human-readable string of direct connections for debugging."""
        if node_id not in self.G:
            return ""

        diagnostics = []
        # Incoming edges (What points to this node)
        for pred in self.G.predecessors(node_id):
            edge_data = self.G.edges[pred, node_id]
            pred_type = self.G.nodes[pred].get("WorkItemType", "Unknown")
            diagnostics.append(f"<- [{_edge_label(edge_data)}] from {pred_type} ({pred})")

        # Outgoing edges (What this node points to)
        for succ in self.G.successors(node_id):
            edge_data = self.G.edges[node_id, succ]
            succ_type = self.G.nodes[succ].get("WorkItemType", "Unknown")
            diagnostics.append(f"-> [{_edge_label(edge_data)}] to {succ_type} ({succ})")

        return " | ".join(diagnostics) if diagnostics else "Isolated Node"

    def _nodes_subset(self, node_ids: list) -> pd.DataFrame:
        """Generates full data-dumps for subsets enriched with debugging metrics."""
        df = self.df_nodes[self.df_nodes["WI_ID"].isin(node_ids)].copy().reset_index(drop=True)
        if df.empty:
            return df

        # Inject the debugging metrics requested
        df["WI_URL"] = df["WI_ID"].apply(self._wi_url)
        df["Graph_Neighbors_Debug"] = df["WI_ID"].apply(self._get_neighborhood_diagnostic)

        # Shift structural validation metrics right next to the Title column for readability
        cols = list(df.columns)
        if "Title" in cols:
            title_idx = cols.index("Title")
            cols.insert(title_idx + 1, cols.pop(cols.index("WI_URL")))
            cols.insert(title_idx + 2, cols.pop(cols.index("Graph_Neighbors_Debug")))
            df = df[cols]
        else:
            wi_id_idx = cols.index("WI_ID")
            cols.insert(wi_id_idx + 1, cols.pop(cols.index("WI_URL")))
            cols.insert(wi_id_idx + 2, cols.pop(cols.index("Graph_Neighbors_Debug")))
            df = df[cols]

        return df

    def find_cycles(self) -> pd.DataFrame:
        cycles = list(nx.simple_cycles(self.G))
        rows = []
        for i, c in enumerate(cycles):
            for node in c:
                row = {"Cycle_Index": i, "Node_ID": node}
                row.update(self._meta(node))
                rows.append(row)
        log.info("Cycles found: %d", len(cycles))
        if not rows:
            return pd.DataFrame(
                columns=[
                    "Cycle_Index",
                    "Node_ID",
                    "Title",
                    "WI_URL",
                    "State",
                    "Roadmap_State",
                    "WorkItemType",
                    "AreaPath",
                    "IterationPath",
                ]
            )
        return pd.DataFrame(rows)

    def find_orphans(self) -> pd.DataFrame:
        orphan_ids = [
            n for n in self.G.nodes if self.G.in_degree(n) == 0 and self.G.out_degree(n) == 0
        ]
        log.info("Orphans found: %d", len(orphan_ids))
        return self._nodes_subset(orphan_ids)

    def find_no_cr_dep(self) -> pd.DataFrame:
        reachable = self._cr_reachable_set()
        no_dep = [n for n in self.G.nodes if n not in reachable]
        log.info("WIs without CR dependency: %d", len(no_dep))
        return self._nodes_subset(no_dep)

    def find_has_cr_dep(self) -> pd.DataFrame:
        reachable = self._cr_reachable_set()
        has_dep = [n for n in self.G.nodes if n in reachable]
        log.info("WIs with CR dependency: %d", len(has_dep))
        return self._nodes_subset(has_dep)

    def find_longest_chain(self) -> pd.DataFrame:
        cond = nx.condensation(self.G)
        if len(cond) == 0:
            log.info("Longest chain (condensed DAG hops): 0")
            return pd.DataFrame(
                columns=[
                    "Chain_Rank",
                    "SCC_Index",
                    "WI_ID",
                    "Title",
                    "WI_URL",
                    "State",
                    "Roadmap_State",
                    "WorkItemType",
                    "AreaPath",
                    "IterationPath",
                ]
            )

        length = nx.dag_longest_path_length(cond)
        path_scc_indices = nx.dag_longest_path(cond)
        mapping: dict = cond.graph.get("mapping", {})
        scc_members: dict[int, list] = {}
        for node, scc_idx in mapping.items():
            scc_members.setdefault(scc_idx, []).append(node)
        rows = []
        for rank, scc_idx in enumerate(path_scc_indices):
            for wi_id in scc_members.get(scc_idx, []):
                row = {"Chain_Rank": rank, "SCC_Index": scc_idx, "WI_ID": wi_id}
                row.update(self._meta(wi_id))
                rows.append(row)
        log.info("Longest chain (condensed DAG hops): %d", length)
        df = (
            pd.DataFrame(rows)
            if rows
            else pd.DataFrame(
                columns=[
                    "Chain_Rank",
                    "SCC_Index",
                    "WI_ID",
                    "Title",
                    "WI_URL",
                    "State",
                    "Roadmap_State",
                    "WorkItemType",
                    "AreaPath",
                    "IterationPath",
                ]
            )
        )
        df.attrs["longest_path_length"] = length
        return df

    def find_scc(self) -> pd.DataFrame:
        sccs = list(nx.strongly_connected_components(self.G))
        rows = []
        for i, scc in enumerate(sccs):
            for node in scc:
                row = {"SCC_Index": i, "WI_ID": node, "SCC_Size": len(scc)}
                row.update(self._meta(node))
                rows.append(row)
        log.info("SCCs found: %d", len(sccs))
        if not rows:
            return pd.DataFrame(
                columns=[
                    "SCC_Index",
                    "WI_ID",
                    "SCC_Size",
                    "Title",
                    "WI_URL",
                    "State",
                    "Roadmap_State",
                    "WorkItemType",
                    "AreaPath",
                    "IterationPath",
                ]
            )
        return pd.DataFrame(rows)

    def compute_metrics(self) -> pd.DataFrame:
        bc = nx.betweenness_centrality(self.G, normalized=True)
        cond = nx.condensation(self.G)
        mapping: dict = cond.graph.get("mapping", {})
        sources = [n for n in cond.nodes if cond.in_degree(n) == 0]
        scc_depth: dict[int, int] = {n: 0 for n in cond.nodes}
        for src in sources:
            for scc_idx, dist in nx.single_source_shortest_path_length(cond, src).items():
                if dist > scc_depth.get(scc_idx, 0):
                    scc_depth[scc_idx] = dist
        rows = []
        for node in self.G.nodes:
            row = {
                "WI_ID": node,
                "In_Degree": self.G.in_degree(node),
                "Out_Degree": self.G.out_degree(node),
                "Betweenness_Centrality": round(bc.get(node, 0.0), 6),
                "Dependency_Depth": scc_depth.get(mapping.get(node, -1), 0),
            }
            row.update(self._meta(node))
            rows.append(row)
        return pd.DataFrame(rows)

    def _cr_reachable_set(self) -> set:
        cr_nodes = {
            n for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        }
        if not cr_nodes:
            return set()

        rev = self.G.reverse(copy=False)
        reachable: set = set(cr_nodes)
        for cr in cr_nodes:
            reachable.update(nx.descendants(rev, cr))
        return reachable

    def analyze_cr_scope_and_impact(self) -> pd.DataFrame:
        cr_nodes = [
            (n, d) for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        ]
        rows = []
        has_effort = self.effort_field in self.df_nodes.columns
        effort_label = self.effort_field.split(".")[-1] if self.effort_field else "Effort"

        for cr_id, data in cr_nodes:
            descendants = list(nx.descendants(self.G, cr_id))
            total_effort = 0.0
            if has_effort:
                subset = self.df_nodes[self.df_nodes["WI_ID"].isin([cr_id] + descendants)]
                total_effort = (
                    pd.to_numeric(subset[self.effort_field], errors="coerce").fillna(0).sum()
                )

            row = {
                "CR_ID": cr_id,
                "CR_Title": data.get("Title", ""),
                f"Total_{effort_label}": total_effort,
                "Impacted_WI_Count": len(descendants),
                "Impacted_WI_IDs": ",".join(map(str, descendants)),
            }
            row.update(self._meta(cr_id, prefix="CR_"))
            rows.append(row)

        sort_col = f"Total_{effort_label}" if has_effort else "Impacted_WI_Count"
        if not rows:
            return pd.DataFrame(
                columns=["CR_ID", "CR_Title", sort_col, "Impacted_WI_Count", "Impacted_WI_IDs"]
            )
        return pd.DataFrame(rows).sort_values(sort_col, ascending=False)

    def analyze_cr_prerequisites(self) -> pd.DataFrame:
        cr_nodes = [
            (n, d) for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        ]
        rows = []
        for cr_id, data in cr_nodes:
            prereqs = list(nx.ancestors(self.G, cr_id))
            row = {
                "CR_ID": cr_id,
                "CR_Title": data.get("Title", ""),
                "Prerequisite_WI_Count": len(prereqs),
                "Prerequisite_WI_IDs": ",".join(map(str, prereqs)),
            }
            row.update(self._meta(cr_id, prefix="CR_"))
            rows.append(row)
        if not rows:
            return pd.DataFrame(
                columns=["CR_ID", "CR_Title", "Prerequisite_WI_Count", "Prerequisite_WI_IDs"]
            )
        return pd.DataFrame(rows).sort_values("Prerequisite_WI_Count", ascending=False)

    def analyze_cr_impact_unpacked(self) -> pd.DataFrame:
        """Flat 1:1 view of CRs and every individual downstream impact WI."""
        cr_nodes = [
            n for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        ]
        rows = []
        for cr_id in cr_nodes:
            cr_meta = self._meta(cr_id, prefix="CR_")
            cr_title = self.G.nodes[cr_id].get("Title", "")
            for desc_id in nx.descendants(self.G, cr_id):
                row = {"CR_ID": cr_id, "CR_Title": cr_title}
                row.update(cr_meta)
                row.update(
                    {
                        "Impacted_WI_ID": desc_id,
                        "Impacted_Title": self.G.nodes[desc_id].get("Title", ""),
                    }
                )
                row.update(self._meta(desc_id, prefix="Impacted_"))
                rows.append(row)
        log.info("Generated unpacked impact view (%d rows).", len(rows))
        if not rows:
            return pd.DataFrame(columns=["CR_ID", "CR_Title", "Impacted_WI_ID", "Impacted_Title"])
        return pd.DataFrame(rows)

    def analyze_cr_prereqs_unpacked(self) -> pd.DataFrame:
        """Flat 1:1 view of CRs and every individual upstream prerequisite WI."""
        cr_nodes = [
            n for n, d in self.G.nodes(data=True) if d.get("WorkItemType") == "Change Request"
        ]
        rows = []
        for cr_id in cr_nodes:
            cr_meta = self._meta(cr_id, prefix="CR_")
            cr_title = self.G.nodes[cr_id].get("Title", "")
            for asc_id in nx.ancestors(self.G, cr_id):
                row = {"CR_ID": cr_id, "CR_Title": cr_title}
                row.update(cr_meta)
                row.update(
                    {"Prereq_WI_ID": asc_id, "Prereq_Title": self.G.nodes[asc_id].get("Title", "")}
                )
                row.update(self._meta(asc_id, prefix="Prereq_"))
                rows.append(row)
        log.info("Generated unpacked prerequisites view (%d rows).", len(rows))
        if not rows:
            return pd.DataFrame(columns=["CR_ID", "CR_Title", "Prereq_WI_ID", "Prereq_Title"])
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Analyze pipeline
# ---------------------------------------------------------------------------

ALL_STEPS = (
    "cycles",
    "orphans",
    "no_cr_dep",
    "has_cr_dep",
    "longest_chain",
    "scc",
    "metrics",
    "cr_scope_impact",
    "cr_prereqs",
    "cr_impact_unpacked",
    "cr_prereqs_unpacked",
)


def run_analyze(args: argparse.Namespace) -> None:
    """Entry point for `analyze` mode."""
    df_nodes = pd.read_parquet(args.nodes_file)
    df_edges = pd.read_parquet(args.edges_file)
    log.info("Loaded %d nodes, %d edges.", len(df_nodes), len(df_edges))

    steps_requested = (
        list(ALL_STEPS)
        if args.steps.strip().lower() == "all"
        else [s.strip().lower() for s in args.steps.split(",")]
    )

    try:
        analyzer = GraphAnalyzer(
            df_nodes,
            df_edges,
            args.ado_url,
            args.effort_field,
            bridges=args.bridges,
            max_indirect_edges=args.max_indirect_edges,
        )
    except ContractionBudgetExceeded as exc:
        # Raised before any output is created.
        raise SystemExit(f"error: {exc}") from None
    step_fn_map = {
        "cycles": analyzer.find_cycles,
        "orphans": analyzer.find_orphans,
        "no_cr_dep": analyzer.find_no_cr_dep,
        "has_cr_dep": analyzer.find_has_cr_dep,
        "longest_chain": analyzer.find_longest_chain,
        "scc": analyzer.find_scc,
        "metrics": analyzer.compute_metrics,
        "cr_scope_impact": analyzer.analyze_cr_scope_and_impact,
        "cr_prereqs": analyzer.analyze_cr_prerequisites,
        "cr_impact_unpacked": analyzer.analyze_cr_impact_unpacked,
        "cr_prereqs_unpacked": analyzer.analyze_cr_prereqs_unpacked,
    }

    results: dict[str, pd.DataFrame] = {}
    for step in steps_requested:
        fn = step_fn_map.get(step)
        if fn is None:
            log.warning("Unknown analysis step '%s' – skipping.", step)
            continue
        log.info("Running step: %s …", step)
        results[step] = fn()

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for sheet_name, df in results.items():
            safe_name = sheet_name[:31]
            df.to_excel(writer, sheet_name=safe_name, index=False)
            # Auto-adjust column widths for better UX
            worksheet = writer.sheets[sheet_name]
            for column in worksheet.columns:
                max_length = 0
                column_letter = get_column_letter(column[0].column)
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except Exception:
                        pass
                adjusted_width = (max_length + 2) if max_length < 50 else 50
                worksheet.column_dimensions[column_letter].width = adjusted_width
            log.info("  → sheet '%s' (%d rows)", safe_name, len(df))

    log.info("Analysis report written to %s", output_path)


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


if __name__ == "__main__":
    main()
