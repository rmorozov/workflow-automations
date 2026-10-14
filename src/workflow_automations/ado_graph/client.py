"""ADO access: CA bundle, Kerberos-authenticated SDK client and WIQL builders."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import certifi
from azure.devops.connection import Connection
from azure.devops.v7_0.work_item_tracking.models import (
    TeamContext,
    Wiql,
    WorkItemBatchGetRequest,
)
from msrest.authentication import KerberosAuthentication
from requests_kerberos import OPTIONAL

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
        Takes a list of IDs and returns the subset that must still be fetched:
        IDs matching the project and type filters, plus IDs WIQL does not
        return at all (deleted or inaccessible).  The batch fetch then reports
        the latter as omitted, so a missing work item is never counted as
        filtered out.
        """
        valid_ids = set()

        # Process in chunks of 1000 to avoid exceeding WIQL string length limits
        for chunk in _iter_chunks(ids, WIQL_FILTER_CHUNK):
            wiql = _wiql_ids(chunk)

            # System.TeamProject is the native, fully indexed way to filter projects
            # Note: WIQL escapes single quotes by doubling them ('')
            if include_projects:
                projs = ", ".join("'{}'".format(p.replace("'", "''")) for p in include_projects)
                wiql += f" AND [System.TeamProject] IN ({projs})"

            if skip_types:
                types = ", ".join("'{}'".format(t.replace("'", "''")) for t in skip_types)
                wiql += f" AND [System.WorkItemType] NOT IN ({types})"

            try:
                accepted = self._query_ids(wiql)
            except Exception as e:
                log.warning("WIQL filter chunk failed (skipping filter for chunk): %s", e)
                # Fallback: assume all are valid if query fails, BFS will handle them natively
                valid_ids.update(chunk)
                continue
            valid_ids.update(accepted)

            # WIQL returns only existing, readable items, so a rejected ID may be
            # missing rather than filtered.  Keep those for the batch fetch.
            rejected = [wi_id for wi_id in chunk if wi_id not in accepted]
            if not rejected:
                continue
            try:
                existing = self._query_ids(_wiql_ids(rejected))
            except Exception as e:
                log.warning("WIQL existence check failed (fetching rejected IDs): %s", e)
                existing = set()
            valid_ids.update(wi_id for wi_id in rejected if wi_id not in existing)

        return valid_ids

    def _query_ids(self, wiql: str) -> set[int]:
        res = self.wit.query_by_wiql(wiql=Wiql(query=wiql), top=ADO_WIQL_PAGE)
        return {ref.id for ref in res.work_items or []}

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


def _wiql_ids(ids: list[int]) -> str:
    id_list = ",".join(map(str, ids))
    return f"SELECT [System.Id] FROM WorkItems WHERE [System.Id] IN ({id_list})"


def _iter_chunks(lst: list, size: int) -> Iterator[list]:
    for i in range(0, len(lst), size):
        yield lst[i : i + size]
