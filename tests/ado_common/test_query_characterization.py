"""Characterization of how ado-graph and ado-triage read saved queries and batch fetches.

Written before the shared ID extraction and chunking moved into ado_common; both
tools must behave the same before and after. Each tool keeps its own omission
policy: graph asks the batch API to omit missing items, triage requires every core
item and only tolerates missing linked or parent items.
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

azure_connection = pytest.importorskip("azure.devops.connection")
pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from workflow_automations import ado_triage as triage  # noqa: E402
from workflow_automations.ado_graph import client as graph  # noqa: E402


def ref(wi_id):
    return SimpleNamespace(id=wi_id)


def link(source, target):
    return SimpleNamespace(
        source=None if source is None else ref(source),
        target=None if target is None else ref(target),
    )


# --- ado-graph ---


def graph_client(wit):
    client = object.__new__(graph.ADOClient)
    client._wit_client = wit
    return client


def graph_query_ids(result):
    return graph_client(SimpleNamespace(query_by_id=lambda **kw: result)).run_query_by_id(
        "Example", "q"
    )


def test_graph_flat_result_keeps_query_order():
    result = SimpleNamespace(work_items=[ref(3), ref(1), ref(2)], work_item_relations=None)
    assert graph_query_ids(result) == [3, 1, 2]


def test_graph_link_result_lists_each_endpoint_once_in_first_seen_order():
    result = SimpleNamespace(
        work_items=None,
        work_item_relations=[link(None, 7), link(7, 3), link(7, 9), link(3, 9), link(None, 4)],
    )
    assert graph_query_ids(result) == [7, 3, 9, 4]


def test_graph_prefers_flat_items_when_a_result_has_both():
    result = SimpleNamespace(work_items=[ref(1)], work_item_relations=[link(1, 2)])
    assert graph_query_ids(result) == [1]


def test_graph_empty_result_has_no_ids():
    assert graph_query_ids(SimpleNamespace(work_items=[], work_item_relations=[])) == []


def test_graph_batch_request_omits_missing_items_and_expands_relations():
    requests = []
    wit = SimpleNamespace(get_work_items_batch=lambda req: requests.append(req) or None)
    client = graph_client(wit)

    assert client.get_work_items_batch([]) == []
    assert requests == []
    assert client.get_work_items_batch([1, 2]) == []
    assert requests[0].ids == [1, 2]
    assert requests[0].error_policy == "Omit"
    assert requests[0].expand == "Relations"


def test_graph_wiql_filter_chunks_by_1000_and_keeps_a_failed_chunk():
    """Rejected IDs get one existence query; missing ones stay for the fetch (AG-003)."""
    queries = []

    def query_by_wiql(wiql, top):
        filtered = "WorkItemType" in wiql.query
        queries.append(filtered)
        if len(queries) == 3:
            raise RuntimeError("too long")
        ids = [int(i) for i in wiql.query.split("IN (")[1].split(")")[0].split(",")]
        # Even IDs pass the filter; IDs above 2400 do not exist.
        found = [i for i in ids if i <= 2400 and (i % 2 == 0 or not filtered)]
        return SimpleNamespace(work_items=[ref(i) for i in found])

    client = graph_client(SimpleNamespace(query_by_wiql=query_by_wiql))
    ids = list(range(1, 2502))

    valid = client.filter_ids_via_wiql(ids, frozenset(), frozenset({"task"}))

    assert queries == [True, False, True, True, False]
    assert valid == (
        {i for i in range(1, 1001) if i % 2 == 0}
        | set(range(1001, 2001))
        | {i for i in range(2001, 2401) if i % 2 == 0}
        | set(range(2401, 2502))
    )


def test_graph_batch_limit_is_200():
    assert graph.ADO_BATCH_LIMIT == 200


# --- ado-triage ---


class TriageClient:
    def __init__(self, result, store):
        self.result, self.store, self.calls = result, store, []

    def query_by_id(self, id):
        return self.result

    def get_work_items(self, ids, fields=None, expand=None, error_policy=None):
        self.calls.append({"ids": list(ids), "fields": fields, "error_policy": error_policy})
        if error_policy == "omit":
            return [self.store.get(i) for i in ids]
        return [self.store[i] for i in ids if i in self.store]


@pytest.fixture
def triage_ado(monkeypatch):
    kerberos = ModuleType("requests_kerberos")
    kerberos.OPTIONAL = "optional"
    kerberos.HTTPKerberosAuth = type("HTTPKerberosAuth", (), {"__init__": lambda self, **kw: None})
    monkeypatch.setitem(sys.modules, "requests_kerberos", kerberos)

    def install(client):
        wit = SimpleNamespace(get_work_item_tracking_client=lambda: client)
        connection = lambda base_url, creds: SimpleNamespace(clients=wit)  # noqa: E731
        monkeypatch.setattr(azure_connection, "Connection", connection)
        return client

    return install


def wi(wi_id, fields=None, relations=()):
    return SimpleNamespace(id=wi_id, fields=fields or {}, relations=list(relations))


def test_triage_reads_flat_items_and_link_endpoints_together(triage_ado):
    store = {i: wi(i) for i in (1, 2, 3, 4)}
    result = SimpleNamespace(
        work_items=[ref(4), ref(2)], work_item_relations=[link(None, 3), link(3, 2), link(3, 1)]
    )
    client = triage_ado(TriageClient(result, store))

    snap = triage.fetch_snapshot("https://ado.example.com/c/p", "q")

    assert sorted(i["id"] for i in snap.items) == [1, 2, 3, 4]
    assert client.calls[0]["ids"] == [1, 2, 3, 4]


def test_triage_core_fetch_sorts_and_batches_by_200_without_omission(triage_ado):
    store = {i: wi(i) for i in range(1, 402)}
    result = SimpleNamespace(work_items=[ref(i) for i in reversed(store)], work_item_relations=None)
    client = triage_ado(TriageClient(result, store))

    triage.fetch_snapshot("https://ado.example.com/c/p", "q")

    assert [c["ids"][0] for c in client.calls] == [1, 201, 401]
    assert [len(c["ids"]) for c in client.calls] == [200, 200, 1]
    assert {c["error_policy"] for c in client.calls} == {None}


def test_triage_missing_core_item_is_fatal(triage_ado):
    result = SimpleNamespace(work_items=[ref(1), ref(2)], work_item_relations=None)
    triage_ado(TriageClient(result, {1: wi(1)}))
    with pytest.raises(triage.AdoError, match="1 queried work items could not be fetched"):
        triage.fetch_snapshot("https://ado.example.com/c/p", "q")


def test_triage_linked_items_are_omitted_in_batches_and_reported(triage_ado):
    url = "https://ado.example.com/c/p/_apis/wit/workItems/{}"
    relations = [
        SimpleNamespace(rel=triage.REL_SUCCESSOR, url=url.format(i)) for i in range(100, 350)
    ]
    store = {1: wi(1, relations=relations)}
    store.update({i: wi(i) for i in range(100, 349)})
    result = SimpleNamespace(work_items=[ref(1)], work_item_relations=None)
    client = triage_ado(TriageClient(result, store))

    snap = triage.fetch_snapshot("https://ado.example.com/c/p", "q")

    linked = [c for c in client.calls if c["error_policy"] == "omit"]
    assert [len(c["ids"]) for c in linked] == [200, 50]
    assert len(snap.external) == 249
    assert snap.warnings == ["1 linked items unavailable (deleted or no permission)"]
