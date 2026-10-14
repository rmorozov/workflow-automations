"""Offline behavior tests for the ado-graph ADO client using SDK-like fakes."""

import os
import re
from types import SimpleNamespace

import pytest

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


# ---------------------------------------------------------------------------
# AG-008: WIQL quoting and keyset pagination
# ---------------------------------------------------------------------------

LITERAL = re.compile(r"'((?:[^']|'')*)'")


def literals(text):
    """WIQL string literals of *text*, unescaped."""
    return [m.replace("''", "'") for m in LITERAL.findall(text)]


def outside_literals(text):
    return LITERAL.sub("''", text)


class FakeWiql:
    """query_by_wiql over synthetic items {id: (project, type)}.

    Parses the clauses the client builds, reading names only from string
    literals, so a quoting slip changes the result.  ``ignore_keyset`` models a
    server that does not honour the ``[System.Id] >`` clause.
    """

    def __init__(self, items, ignore_keyset=False):
        self.items = items
        self.ignore_keyset = ignore_keyset
        self.queries = []

    def query_by_wiql(self, wiql, top):
        text = wiql.query
        self.queries.append(text)
        bare = outside_literals(text)
        assert bare.count("ORDER BY") == 1 and bare.rstrip().endswith("ORDER BY [System.Id] ASC")
        ids = set(map(int, re.search(r"\[System.Id\] IN \(([\d,]+)\)", text).group(1).split(",")))
        last = int(re.search(r"\[System.Id\] > (\d+)", bare).group(1))
        projects = types = None
        clauses = re.split(r" AND (?=\[)", text)
        for clause in clauses:
            if clause.startswith("[System.TeamProject] IN"):
                projects = set(literals(clause))
            elif clause.startswith("[System.WorkItemType] NOT IN"):
                types = {t.lower() for t in literals(clause)}
        found = sorted(
            i
            for i, (project, wi_type) in self.items.items()
            if i in ids
            and (projects is None or project in projects)
            and (types is None or wi_type.lower() not in types)
            and (self.ignore_keyset or i > last)
        )
        return SimpleNamespace(work_items=[SimpleNamespace(id=i) for i in found[:top]])


def wiql_client(fake):
    ado = object.__new__(tool.ADOClient)
    ado._wit_client = fake
    return ado


@pytest.mark.parametrize(
    "name",
    ["O'Brien", "It''s", "Team ORDER BY [System.Id]", "a') OR ([System.Id] > 0", "ORDER"],
)
def test_filter_quotes_apostrophes_and_order_in_names(name):
    items = {1: (name, "Epic"), 2: ("Other", "Epic"), 3: (name, name), 4: (name, "Task")}
    fake = FakeWiql(items)
    valid = wiql_client(fake).filter_ids_via_wiql(
        [1, 2, 3, 4], frozenset({name}), frozenset({name.lower(), "task"})
    )
    # 2 and 4 are rejected by the filter but exist, so they are pruned.
    assert valid == {1}
    assert name in literals(fake.queries[0])


def test_wiql_string_doubles_single_quotes():
    assert tool.wiql_string("O'Brien's") == "'O''Brien''s'"
    assert literals(tool.wiql_string("a''b ORDER")) == ["a''b ORDER"]


def test_query_ids_pages_past_the_wiql_page_limit(monkeypatch):
    monkeypatch.setattr(tool, "ADO_WIQL_PAGE", 20_000)
    ids = list(range(1, 45_002))
    fake = FakeWiql({i: ("P", "Epic") for i in ids})
    found = wiql_client(fake)._query_ids(tool._wiql_ids(ids))
    assert found == set(ids)
    assert [re.search(r"> (\d+)", q).group(1) for q in fake.queries] == ["0", "20000", "40000"]


def test_query_ids_full_last_page_needs_one_empty_page(monkeypatch):
    monkeypatch.setattr(tool, "ADO_WIQL_PAGE", 3)
    fake = FakeWiql({i: ("P", "Epic") for i in range(1, 7)})
    assert wiql_client(fake)._query_ids(tool._wiql_ids(list(range(1, 7)))) == set(range(1, 7))
    assert len(fake.queries) == 3


def test_non_advancing_page_raises_instead_of_looping(monkeypatch):
    monkeypatch.setattr(tool, "ADO_WIQL_PAGE", 2)
    fake = FakeWiql({i: ("P", "Epic") for i in range(1, 6)}, ignore_keyset=True)
    with pytest.raises(RuntimeError, match="did not advance"):
        wiql_client(fake)._query_ids(tool._wiql_ids(list(range(1, 6))))
    assert len(fake.queries) == 2


def test_non_advancing_filter_page_falls_back_to_fetching_the_chunk(monkeypatch):
    monkeypatch.setattr(tool, "ADO_WIQL_PAGE", 2)
    fake = FakeWiql({i: ("P", "Task") for i in range(1, 6)}, ignore_keyset=True)
    valid = wiql_client(fake).filter_ids_via_wiql(list(range(1, 6)), frozenset({"P"}), frozenset())
    assert valid == set(range(1, 6))


@pytest.mark.skipif(
    not (os.environ.get("ADO_CONTRACT_URL") and os.environ.get("ADO_CONTRACT_PROJECT")),
    reason="set ADO_CONTRACT_URL and ADO_CONTRACT_PROJECT to run against a real server",
)
def test_contract_real_server_accepts_quoted_names_and_paging():
    """Optional: the generated WIQL parses on a real server and paging terminates."""
    pytest.importorskip("azure.devops.connection")
    project = os.environ["ADO_CONTRACT_PROJECT"]
    ado = tool.ADOClient(os.environ["ADO_CONTRACT_URL"])
    where = f"[System.TeamProject] = {tool.wiql_string(project)}"
    found = ado._query_ids(where)
    assert all(isinstance(i, int) for i in found)
    quoted = f"[System.TeamProject] = {tool.wiql_string(project + chr(39) + ' ORDER BY x')}"
    assert ado._query_ids(quoted) == set()
