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

Work-item type filtering (export mode):
  --skip-types  comma-separated list of ADO WorkItemType values to exclude from
                the output DataFrames.  Skipped items are still traversed so
                their relations can lead to accepted items.
  Default skip list: Task, Bug, Test Case, Test Suite, Test Plan, Test Run,
                     Shared Steps, Shared Parameter.
  Pass --skip-types "" to disable all filtering.

ADO project filtering (export mode):
  --include-projects  comma-separated list of ADO project names to include
                      (case-insensitive, matched against System.TeamProject).
                      Work items from other projects are excluded from
                      nodes/edges but still traversed.
  When omitted, ALL projects are accepted.

BFS optimisation — WIQL pre-seed index (WIIndex):
  When --include-projects is given, the tool runs cheap WIQL SELECT [System.Id]
  queries (once per included project) BEFORE the BFS starts.  This populates a
  local ID → classification lookup so that relation targets can be resolved
  without an ADO round-trip:

    • IDs in included projects with an accepted type  → queued for full fetch
    • IDs in included projects with a skipped type    → counted in stats,
                                                        dropped from fetch batch
    • IDs discovered from excluded projects           → counted in stats,
                                                        dropped from fetch batch

  Net effect: the BFS only calls get_work_items_batch for WIs that will
  actually produce output or whose relations are unknown.  On a graph of
  50 000 WIs with 30 % filtered out, this eliminates roughly 150 batch
  round-trips.

Export statistics:
  After each BFS batch a live line is logged showing visited / added /
  skipped / excluded / queued counts.
  A formatted per-type and per-project table is printed when BFS completes.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import re
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
        for chunk in _iter_chunks(ids, 1000):
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
    # Ad-hoc WIQL (returns IDs only, used by WIIndex pre-seed)
    # ------------------------------------------------------------------

    def query_wiql_ids(self, wiql_text: str, project: str = "") -> list[int]:
        """
        Execute an arbitrary WIQL string and return all matching WI IDs.

        ADO caps query_by_wiql results at ADO_WIQL_PAGE (20 000) per call.
        We implement ID-keyset pagination: after each page we inject
        `AND [System.Id] > <last_id>` and re-query until a page arrives
        with fewer rows than the page size.

        The WIQL must include `ORDER BY [System.Id] ASC` (or no ORDER BY)
        for keyset pagination to be correct; _build_wiql_for_project enforces
        this.
        """
        team_context = TeamContext(project=project) if project else None
        all_ids: list[int] = []
        base_wiql = wiql_text
        last_id = 0

        while True:
            current_wiql = _wiql_append_id_gt(base_wiql, last_id) if last_id else base_wiql
            result = self.wit.query_by_wiql(
                wiql=Wiql(query=current_wiql),
                team_context=team_context,
                top=ADO_WIQL_PAGE,
            )
            page = [ref.id for ref in (result.work_items or [])]
            all_ids.extend(page)

            if len(page) < ADO_WIQL_PAGE:
                break  # last page reached

            last_id = page[-1]
            log.debug("WIQL page: %d ids, continuing from id > %d", len(page), last_id)

        return all_ids

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

    def get_work_item_types_batch(self, ids: list[int]) -> dict[int, str]:
        """
        Fetch only System.WorkItemType for a list of IDs.
        Used by WIIndex to learn the type of skipped-type IDs cheaply
        (no relations expansion, minimal payload).
        Returns {wi_id: WorkItemType}.
        """
        if not ids:
            return {}
        result: dict[int, str] = {}
        for chunk in _iter_chunks(ids, ADO_BATCH_LIMIT):
            req = WorkItemBatchGetRequest(
                ids=chunk,
                fields=["System.Id", "System.WorkItemType"],
            )
            items = self.wit.get_work_items_batch(req) or []
            for wi in items:
                wi_type = (wi.fields or {}).get("System.WorkItemType", "Unknown")
                result[wi.id] = wi_type
        return result

    def get_available_fields(self) -> list[str]:
        """Return all field reference names defined in the collection."""
        fields = self.wit.get_fields()
        return [f.reference_name for f in (fields or [])]


# ---------------------------------------------------------------------------
# WIQL helpers
# ---------------------------------------------------------------------------


def _wiql_append_id_gt(wiql: str, last_id: int) -> str:
    """
    Inject `AND [System.Id] > <last_id>` for keyset pagination.

    Inserts before the ORDER BY clause if one is present, otherwise appends.
    Handles both upper and mixed case ORDER BY.

    Example:
        SELECT [System.Id] FROM WorkItems
        WHERE [System.TeamProject] = 'Foo'
        ORDER BY [System.Id] ASC
      → (last_id=5000)
        SELECT [System.Id] FROM WorkItems
        WHERE [System.TeamProject] = 'Foo'
        AND [System.Id] > 5000
        ORDER BY [System.Id] ASC
    """
    clause = f" AND [System.Id] > {last_id}"
    # Case-insensitive search for ORDER
    match = re.search(r"\bORDER\b", wiql, re.IGNORECASE)
    if match:
        pos = match.start()
        return wiql[:pos].rstrip() + clause + " " + wiql[pos:]
    return wiql.rstrip() + clause


def _build_wiql_for_project(
    project: str,
    skip_types: frozenset[str],
    *,
    include_skipped: bool,
) -> str:
    """
    Build a WIQL SELECT [System.Id] query for *project*.

    When include_skipped=False: selects WIs whose type is NOT in skip_types
    (i.e. the accepted IDs).
    When include_skipped=True: selects WIs whose type IS in skip_types
    (i.e. the type-skipped IDs we want to count in stats).

    The query always ends with ORDER BY [System.Id] ASC so keyset pagination
    is safe.
    """
    proj_safe = project.replace("'", "\\'")

    if not skip_types:
        # No type filter — all WIs in the project are accepted
        return (
            f"SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{proj_safe}' "
            f"ORDER BY [System.Id] ASC"
        )

    types_sql = ", ".join(f"'{t}'" for t in sorted(skip_types))

    if include_skipped:
        type_clause = f"AND [System.WorkItemType] IN ({types_sql})"
    else:
        type_clause = f"AND [System.WorkItemType] NOT IN ({types_sql})"

    return (
        f"SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{proj_safe}' "
        f"{type_clause} "
        f"ORDER BY [System.Id] ASC"
    )


# ---------------------------------------------------------------------------
# WIIndex: WIQL pre-seed for zero-cost BFS classification
# ---------------------------------------------------------------------------


class WIIndex:
    """
    Lightweight ID → classification lookup built from WIQL queries BEFORE
    the BFS starts.

    Why this matters
    ────────────────
    In a large ADO graph, the BFS discovers thousands of relation targets
    belonging to excluded projects or filtered-out types.  Without an index,
    every such ID must be fetched from ADO (occupying a slot in a 200-item
    batch) just to learn it should be discarded — wasting HTTP round-trips
    for zero output value.

    The index makes classification free (O(1) set lookup) for all IDs that
    fall into the included projects.  Concretely:

      • Pre-seed fires 2 WIQL queries per included project:
          Query A – accepted IDs (type NOT IN skip_types)
          Query B – skipped-type IDs (type IN skip_types, type field fetched
                    in cheap fields-only batches, no relations expansion)
        Both queries return only [System.Id], evaluated server-side.
        Total requests: 2×P WIQL + ceil(|skipped_ids| / 200) batch calls.

      • During BFS, before each fetch batch, we sweep the candidate IDs:
          – known-accepted     → goes into the fetch batch as normal
          – known-skipped-type → counted in stats, removed from batch
          – known-excl-project → counted in stats, removed from batch
          – unknown            → goes into the fetch batch (fallback path)

    Request budget vs savings (illustrative, 50 000 WIs, 30 % filtered):
      Pre-seed cost : 2 WIQL calls + ~75 batch calls (for skipped-type types)
      BFS savings   : ~75 batch calls that would have fetched filtered WIs
      Break-even    : essentially immediate for any graph > ~2 000 WIs

    Excluded projects
    ─────────────────
    We do NOT pre-enumerate excluded projects (we don't know which projects
    exist outside include_projects without an extra API call).  Instead, when
    BFS fetches a WI from an unknown project and discovers it is excluded, we
    register that ID lazily so future encounters are free.

    Thread safety: not applicable — single-threaded BFS.
    """

    ACCEPTED = "accepted"
    SKIPPED_TYPE = "skipped_type"
    EXCLUDED_PROJECT = "excluded_project"
    UNKNOWN = "unknown"

    def __init__(self) -> None:
        # IDs of WIs that pass both project and type filters
        self._accepted: set[int] = set()
        # IDs of WIs whose type is in skip_types (within included projects)
        self._excl_type: dict[int, str] = {}  # id → WorkItemType
        # IDs of WIs from excluded projects (discovered lazily during BFS)
        self._excl_proj: dict[int, str] = {}  # id → project name
        self._built = False

    # ------------------------------------------------------------------
    # Build phase — called once before BFS
    # ------------------------------------------------------------------

    def build(
        self,
        client: ADOClient,
        include_projects: frozenset[str],
        skip_types: frozenset[str],
    ) -> None:
        """
        Run WIQL pre-seed for every included project and populate the index.
        """
        log.info("WIIndex: pre-seeding from %d project(s) …", len(include_projects))
        total_accepted = 0
        total_skipped = 0

        for proj in sorted(include_projects):
            n_acc, n_skp = self._seed_project(client, proj, skip_types)
            log.info("  %-35s  accepted: %6d  skipped-type: %6d", proj, n_acc, n_skp)
            total_accepted += n_acc
            total_skipped += n_skp

        log.info(
            "WIIndex ready — %d accepted IDs, %d skipped-type IDs pre-classified.",
            total_accepted,
            total_skipped,
        )
        self._built = True

    def _seed_project(
        self,
        client: ADOClient,
        project: str,
        skip_types: frozenset[str],
    ) -> tuple[int, int]:
        """
        Seed one project into the index.

        Query A: accepted IDs (type NOT in skip_types, or all if no filter)
        Query B: skipped-type IDs (type IN skip_types) — then fetch their
                 type names via a fields-only batch so stats show per-type
                 counts.

        Returns (n_accepted, n_skipped_type).
        """
        # ── Query A: accepted IDs ─────────────────────────────────────────
        wiql_a = _build_wiql_for_project(project, skip_types, include_skipped=False)
        accepted_ids = client.query_wiql_ids(wiql_a, project=project)
        self._accepted.update(accepted_ids)

        # ── Query B: skipped-type IDs (only when type filter is active) ───
        skipped_count = 0
        if skip_types:
            wiql_b = _build_wiql_for_project(project, skip_types, include_skipped=True)
            skipped_ids = client.query_wiql_ids(wiql_b, project=project)

            if skipped_ids:
                # Fetch only WorkItemType for these IDs — no relations, cheap.
                type_map = client.get_work_item_types_batch(skipped_ids)
                for wi_id, wi_type in type_map.items():
                    self._excl_type[wi_id] = wi_type
                skipped_count = len(type_map)

        return len(accepted_ids), skipped_count

    # ------------------------------------------------------------------
    # Query phase — called per queued ID during BFS sweep
    # ------------------------------------------------------------------

    def classify(self, wi_id: int) -> str:
        """
        Return one of ACCEPTED / SKIPPED_TYPE / EXCLUDED_PROJECT / UNKNOWN.

        UNKNOWN means the ID was not seen during pre-seed (e.g. it belongs
        to a project outside include_projects that we haven't seen yet, or
        no project filter is active).  The BFS falls through to a normal
        ADO batch fetch for UNKNOWN IDs.
        """
        if not self._built:
            return self.UNKNOWN
        if wi_id in self._accepted:
            return self.ACCEPTED
        if wi_id in self._excl_type:
            return self.SKIPPED_TYPE
        if wi_id in self._excl_proj:
            return self.EXCLUDED_PROJECT
        return self.UNKNOWN

    def type_of(self, wi_id: int) -> str:
        """Return the WorkItemType for a SKIPPED_TYPE ID."""
        return self._excl_type.get(wi_id, "Unknown")

    def project_of(self, wi_id: int) -> str:
        """Return the project name for an EXCLUDED_PROJECT ID."""
        return self._excl_proj.get(wi_id, "Unknown")

    def register_excluded(self, wi_id: int, project: str) -> None:
        """
        Lazily register an ID discovered (via BFS fetch) to belong to an
        excluded project.  Subsequent encounters of this ID are free.
        """
        self._excl_proj[wi_id] = project

    @property
    def size(self) -> int:
        return len(self._accepted) + len(self._excl_type) + len(self._excl_proj)


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
    """

    def __init__(self) -> None:
        self.added: collections.Counter = collections.Counter()
        self.skipped_type: collections.Counter = collections.Counter()
        self.excluded_project: collections.Counter = collections.Counter()

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
            " | excluded(project): %d | queued: %d",
            self.total_visited,
            self.total_added,
            self.total_skipped_type,
            self.total_excluded_project,
            queue_remaining,
        )

    def print_summary(self, queue_remaining: int = 0) -> None:
        """Print formatted per-type and per-project breakdown tables to stdout."""
        print()
        self._print_type_table()
        self._print_project_table()
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

    # NOTE: The upfront WIIndex build has been removed to prevent excessive ADO load.
    # Unknown IDs are now handled natively or pruned lazily via WIQL below.

    visited: set[int] = set()
    queued: dict[int, None] = dict.fromkeys(initial_ids)
    all_node_rows: list[dict] = []
    all_edge_rows: list[dict] = []
    stats = ExportStats()

    log.info("Starting BFS export (batch_size=%d).", batch_size)

    with tqdm(desc="Fetching work items", unit="WI") as pbar:
        while queued:
            # ── PRUNE QUEUE OPTIMIZATION ─────────────────────────────────
            # Dynamically prune unvisited IDs via WIQL to prevent queue bloat.
            # Triggered when queued is at least 3x the batch size.
            if (include_projects or skip_types) and len(queued) >= batch_size * 3:
                # Keep the immediate next batch, prune the rest
                candidates = list(queued.keys())
                ids_to_prune = candidates[batch_size:]

                valid_subset = client.filter_ids_via_wiql(
                    ids_to_prune, include_projects, skip_types
                )

                pruned_count = 0
                for pid in ids_to_prune:
                    if pid not in valid_subset:
                        del queued[pid]
                        visited.add(pid)  # Mark visited so we don't requeue them later
                        pruned_count += 1

                if pruned_count > 0:
                    log.info(" Queue optimized: pruned %d out-of-scope WIs via WIQL.", pruned_count)

            # ── a) Pull next candidate batch ───────────────────────────────
            fetch_ids = []
            for wi_id in list(queued.keys())[:batch_size]:
                del queued[wi_id]
                if wi_id not in visited:
                    visited.add(wi_id)
                    fetch_ids.append(wi_id)

            if not fetch_ids:
                stats.print_progress(len(queued))
                continue

            # ── c) Batch fetch the IDs ─────────────────────────────────────
            raw_items = client.get_work_items_batch(fetch_ids)

            # ── d) Process results ─────────────────────────────────────────
            for wi in raw_items:
                if wi is None:
                    continue
                fields: dict = wi.fields or {}
                wi_type: str = fields.get("System.WorkItemType", "Unknown")
                wi_project: str = _project_from_wi(fields)
                node_row, edge_rows = _parse_wi(wi)

                # Enqueue relation targets
                for er in edge_rows:
                    tid: int = er["Target_ID"]
                    if tid not in visited:
                        queued.setdefault(tid, None)

                # Standard filtering
                if include_projects and wi_project.lower() not in include_projects:
                    stats.record_excluded_project(wi_project)
                elif skip_types and wi_type.lower() in skip_types:
                    stats.record_skipped_type(wi_type)
                else:
                    stats.record_added(wi_type)
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
        "Collected %d nodes, %d edges  (skipped by type: %d | excluded by project: %d).",
        len(df_nodes),
        len(df_edges),
        stats.total_skipped_type,
        stats.total_excluded_project,
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
    ) -> None:
        self.df_nodes = df_nodes
        self.df_edges = df_edges
        self.ado_url = ado_url.rstrip("/")
        self.effort_field = effort_field
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
            diagnostics.append(f"<- [{edge_data.get('link_type')}] from {pred_type} ({pred})")

        # Outgoing edges (What this node points to)
        for succ in self.G.successors(node_id):
            edge_data = self.G.edges[node_id, succ]
            succ_type = self.G.nodes[succ].get("WorkItemType", "Unknown")
            diagnostics.append(f"-> [{edge_data.get('link_type')}] to {succ_type} ({succ})")

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

    analyzer = GraphAnalyzer(df_nodes, df_edges, args.ado_url, args.effort_field)
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
        type=int,
        default=200,
        help=f"WI fetch batch size (max {ADO_BATCH_LIMIT}, default 200).",
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
            "Comma-separated ADO WorkItemType values to exclude (case-insensitive).  "
            "Skipped items are still traversed.  Default: %(default)s.  "
            "Pass '' to disable."
        ),
    )
    ep.add_argument(
        "--include-projects",
        default="",
        metavar="PROJECTS",
        help=(
            "Comma-separated ADO project names to include (case-insensitive).  "
            "Work items from other projects are excluded from output but still "
            "traversed.  When set, a WIQL pre-seed index is built to avoid "
            "fetching filtered-out WIs.  Omit to accept all projects."
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
