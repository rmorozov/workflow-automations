"""ADO access: Kerberos-authenticated SDK client and WIQL builders.

The constants are importable without the network extras. The SDK, Kerberos and
certifi are imported only when a client is built (see ado_common.session), so
`analyze` and `--help` run with the offline `ado-graph-analyze` extra alone.
"""

from __future__ import annotations

import logging
from importlib import import_module
from typing import Any

# ADO_BATCH_LIMIT is re-exported for the CLI and export modules.
from workflow_automations.ado_common.query import ADO_BATCH_LIMIT as ADO_BATCH_LIMIT
from workflow_automations.ado_common.query import chunks, flat_result_ids, link_result_ids
from workflow_automations.ado_common.session import connect

log = logging.getLogger("ado_tool")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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


# Modules of the network extra that this module imports lazily.
NETWORK_MODULES: tuple[str, ...] = (
    "certifi",
    "azure.devops.connection",
    "azure.devops.v7_0.work_item_tracking.models",
    "msrest.authentication",
    "requests_kerberos",
)


def import_network_modules() -> None:
    """Import the network dependencies now, so a missing extra fails before any work."""
    for name in NETWORK_MODULES:
        import_module(name)


# ---------------------------------------------------------------------------
# ADOClient  (wraps azure-devops-python-api SDK)
# ---------------------------------------------------------------------------


class ADOClient:
    """
    Thin wrapper around the official azure-devops-python-api SDK.

    Authentication: Kerberos with the current ticket cache and OPTIONAL mutual
    authentication, through ado_common.session.connect().

    Certificate handling: requests verify against *ca_bundle* when given (the
    caller owns its lifetime), otherwise against requests' defaults.
    """

    def __init__(self, base_url: str, ca_bundle: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._connection = connect(self.base_url, ca_bundle=ca_bundle)
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
        from azure.devops.v7_0.work_item_tracking.models import TeamContext

        team_context = TeamContext(project=project) if project else None
        result = self.wit.query_by_id(id=query_id, team_context=team_context)

        return flat_result_ids(result) or link_result_ids(result)

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
        for chunk in chunks(ids, WIQL_FILTER_CHUNK):
            where = _wiql_ids(chunk)

            # System.TeamProject is the native, fully indexed way to filter projects
            if include_projects:
                projs = ", ".join(map(wiql_string, sorted(include_projects)))
                where += f" AND [System.TeamProject] IN ({projs})"

            if skip_types:
                types = ", ".join(map(wiql_string, sorted(skip_types)))
                where += f" AND [System.WorkItemType] NOT IN ({types})"

            try:
                accepted = self._query_ids(where)
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

    def _query_ids(self, where: str) -> set[int]:
        """IDs of the work items matching the WIQL condition *where*.

        query_by_wiql returns at most ADO_WIQL_PAGE rows, so the query is paged
        by ascending ID: each page asks for IDs above the last page's highest.
        The query text is built here, so the keyset clause never has to be
        spliced into caller WIQL.  A page that does not advance past the
        previous one raises instead of looping or returning a truncated set.
        """
        from azure.devops.v7_0.work_item_tracking.models import Wiql

        ids: set[int] = set()
        last = 0
        while True:
            wiql = (
                f"SELECT [System.Id] FROM WorkItems WHERE ({where})"
                f" AND [System.Id] > {last} ORDER BY [System.Id] ASC"
            )
            res = self.wit.query_by_wiql(wiql=Wiql(query=wiql), top=ADO_WIQL_PAGE)
            page = [ref.id for ref in res.work_items or []]
            if any(wi_id <= last for wi_id in page):
                raise RuntimeError(f"WIQL page after ID {last} did not advance")
            ids.update(page)
            if len(page) < ADO_WIQL_PAGE:
                return ids
            last = max(page)

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
        from azure.devops.v7_0.work_item_tracking.models import WorkItemBatchGetRequest

        request = WorkItemBatchGetRequest(ids=ids, expand="Relations", error_policy="Omit")
        return self.wit.get_work_items_batch(request) or []

    def get_available_fields(self) -> list[str]:
        """Return all field reference names defined in the collection."""
        fields = self.wit.get_fields()
        return [f.reference_name for f in (fields or [])]


def wiql_string(value: str) -> str:
    """Quote *value* as a WIQL string literal; WIQL escapes ' by doubling it."""
    return "'{}'".format(value.replace("'", "''"))


def _wiql_ids(ids: list[int]) -> str:
    id_list = ",".join(map(str, ids))
    return f"[System.Id] IN ({id_list})"
