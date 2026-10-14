"""Synthetic frames and SDK-like fakes shared by the ado-graph tests."""

from contextlib import nullcontext
from types import SimpleNamespace

import pandas as pd

from workflow_automations import ado_graph as tool
from workflow_automations.ado_graph import export


def nodes():
    return pd.DataFrame(
        [
            {"WI_ID": 1, "Title": "CR", "WorkItemType": "Change Request", "Effort": 2},
            {"WI_ID": 2, "Title": "Child", "WorkItemType": "Feature", "Effort": 3},
            {"WI_ID": 3, "Title": "Prerequisite", "WorkItemType": "Feature", "Effort": 5},
            {"WI_ID": 4, "Title": "Isolated", "WorkItemType": "Feature", "Effort": 7},
        ]
    )


def edges():
    return pd.DataFrame(
        [(1, 2, "Parent->Child"), (1, 3, "Successor->Predecessor"), (1, 99, "Blocks")],
        columns=["Source_ID", "Target_ID", "Link_Type"],
    )


def wi(wi_id, wi_type="Feature", targets=(), rel="System.LinkTypes.Hierarchy-Forward"):
    return SimpleNamespace(
        id=wi_id,
        fields={"System.WorkItemType": wi_type, "System.TeamProject": "Example"},
        relations=[SimpleNamespace(rel=rel, url=f"https://ado.example/{t}") for t in targets],
    )


def fake_client(items, seeds, rejected=(), log=None):
    """SDK-like client; `rejected` IDs fail the WIQL filter, absent IDs are omitted."""
    log = log if log is not None else {"fetched": [], "filtered": []}

    class Client:
        def __init__(self, _url, ca_bundle=None):
            pass

        def run_query_by_id(self, _project, _query):
            return list(seeds)

        def filter_ids_via_wiql(self, ids, _projects, _types):
            log["filtered"].extend(ids)
            return set(ids) - set(rejected)

        def get_work_items_batch(self, ids):
            log["fetched"].extend(ids)
            return [items[i] for i in ids if i in items]

    return Client, log


def run_export(monkeypatch, tmp_path, client, *extra):
    monkeypatch.setattr(export, "ADOClient", client)
    monkeypatch.setattr(export, "ca_bundle", lambda _dir: nullcontext())
    tool.main(
        [
            "export",
            "--ado-url",
            "https://ado.example",
            "--query-id",
            "synthetic",
            "--output-dir",
            str(tmp_path),
            "--formats",
            "parquet",
            *extra,
        ]
    )
    return pd.read_parquet(tmp_path / "nodes.parquet"), pd.read_parquet(tmp_path / "edges.parquet")


def fan_bridge(fan):
    """Bridge 0 with `fan` in-scope predecessors and `fan` in-scope successors."""
    preds, succs = range(1, fan + 1), range(fan + 1, 2 * fan + 1)
    graph_nodes = pd.DataFrame(
        [{"WI_ID": 0, "WorkItemType": "Task", "In_Scope": False}]
        + [{"WI_ID": i, "WorkItemType": "Feature", "In_Scope": True} for i in [*preds, *succs]]
    )
    links = pd.DataFrame(
        [(p, 0, "Blocks") for p in preds] + [(0, s, "Blocks") for s in succs],
        columns=["Source_ID", "Target_ID", "Link_Type"],
    )
    return graph_nodes, links
