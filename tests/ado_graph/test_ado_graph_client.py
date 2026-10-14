"""Offline behavior tests for the ado-graph ADO client using SDK-like fakes."""

from types import SimpleNamespace

from workflow_automations.ado_graph import client as tool


def test_saved_query_flat_and_link_results():
    client = object.__new__(tool.ADOClient)
    flat = SimpleNamespace(work_items=[SimpleNamespace(id=5)], work_item_relations=None)
    client._wit_client = SimpleNamespace(query_by_id=lambda **kwargs: flat)
    assert client.run_query_by_id("Example", "query") == [5]
    links = SimpleNamespace(
        work_items=None,
        work_item_relations=[
            SimpleNamespace(source=None, target=SimpleNamespace(id=1)),
            SimpleNamespace(source=SimpleNamespace(id=1), target=SimpleNamespace(id=2)),
        ],
    )
    client._wit_client = SimpleNamespace(query_by_id=lambda **kwargs: links)
    assert client.run_query_by_id("Example", "query") == [1, 2]
