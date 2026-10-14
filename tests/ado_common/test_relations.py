"""One registry of ADO relation reference names, adapted to each tool's schema."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from workflow_automations.ado_common import relations as r

URL = "https://ado.example.com/tfs/Collection/_apis/wit/workItems/{}"


def test_registry_names_both_ends_of_every_link_type():
    for ref, link in r.LINK_TYPES.items():
        assert link.reference == ref
        assert ref.endswith("-Forward") == (link.direction == r.FORWARD)
        other = r.LINK_TYPES[link.inverse]
        assert other.inverse == ref
        assert other.family == link.family and other.direction != link.direction
    assert r.LINK_TYPES[r.DEPENDENCY_REVERSE].family == "dependency"
    assert r.LINK_TYPES[r.HIERARCHY_REVERSE].inverse == r.HIERARCHY_FORWARD


@pytest.mark.parametrize("schema", [r.GRAPH, r.TRIAGE], ids=lambda s: s.name)
def test_hierarchy_reverse_is_an_explicit_exclusion(schema):
    assert r.HIERARCHY_REVERSE in r.LINK_TYPES
    assert r.HIERARCHY_REVERSE in schema.excluded
    assert r.HIERARCHY_REVERSE not in schema
    assert schema.endpoint(r.HIERARCHY_REVERSE, URL.format(5)) is None


def test_schemas_label_the_same_link_from_the_emitting_item():
    # Item 1 depends on item 2: graph reads it forward, triage relative to item 1.
    assert r.GRAPH.endpoint(r.DEPENDENCY_REVERSE, URL.format(2)) == ("Successor->Predecessor", 2)
    assert r.TRIAGE.endpoint(r.DEPENDENCY_REVERSE, URL.format(2)) == ("Predecessor", 2)
    assert r.GRAPH.endpoint(r.HIERARCHY_FORWARD, URL.format(3)) == ("Parent->Child", 3)
    assert r.TRIAGE.endpoint(r.HIERARCHY_FORWARD, URL.format(3)) == ("Child", 3)
    # Triage reads no Affects or Implements links; graph reads both ends.
    assert r.TRIAGE.endpoint(r.AFFECTS_FORWARD, URL.format(4)) is None
    assert r.GRAPH.endpoint(r.IMPLEMENTS_REVERSE, URL.format(4)) == ("Implemented By", 4)


@pytest.mark.parametrize(
    ("ref", "url"),
    [
        ("Hyperlink", "https://example.com/page"),
        ("AttachedFile", URL.format("not-a-number")),
        (r.BLOCKS_FORWARD, None),
        (r.BLOCKS_FORWARD, ""),
        (None, URL.format(7)),
        ("", URL.format(7)),
    ],
)
def test_endpoint_skips_unread_types_and_non_work_item_urls(ref, url):
    assert r.GRAPH.endpoint(ref, url) is None
    assert r.TRIAGE.endpoint(ref, url) is None


def test_targets_collects_read_endpoints_only():
    rels = [
        {"rel": r.BLOCKS_FORWARD, "url": URL.format(2)},
        {"rel": r.BLOCKS_REVERSE, "url": URL.format(3) + "/"},
        {"rel": r.AFFECTS_FORWARD, "url": URL.format(4)},
        {"rel": r.HIERARCHY_REVERSE, "url": URL.format(5)},
        {"rel": "Hyperlink", "url": "https://example.com"},
    ]
    assert r.TRIAGE.targets(rels) == {2, 3}
    assert r.GRAPH.targets(rels) == {2, 3, 4}


def test_inverse_labels_pair_reverse_ends_with_their_forward_label():
    assert r.TRIAGE.inverse_labels() == {"Predecessor": "Successor", "Blocked By": "Blocks"}
    assert r.GRAPH.inverse_labels() == {
        "Affected By": "Affects",
        "Blocked By": "Blocks",
        "Successor->Predecessor": "Predecessor->Successor",
        "Implemented By": "Implements",
    }


def test_schema_refuses_unregistered_or_contradictory_names():
    with pytest.raises(ValueError, match="unregistered"):
        r.RelationSchema("x", {"System.LinkTypes.Related": "Related"})
    with pytest.raises(ValueError, match="both"):
        r.RelationSchema(
            "x", {r.HIERARCHY_REVERSE: "Parent"}, excluded=frozenset({r.HIERARCHY_REVERSE})
        )


def test_triage_reads_the_registry():
    pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    from workflow_automations import ado_triage as triage

    assert dict(triage.RELATION_LABELS) == dict(r.TRIAGE.labels)
    assert triage.INVERSE_LABELS == {"Predecessor": "Successor", "Blocked By": "Blocks"}
    assert set(triage.BLAST_RADIUS_LABELS) <= set(triage.RELATION_LABELS.values())


def test_graph_export_emits_registry_labels():
    pytest.importorskip("azure.devops")
    pytest.importorskip("pandas")
    from workflow_automations.ado_graph import export

    rel = SimpleNamespace
    wi = SimpleNamespace(
        id=1,
        fields={"System.Title": "Root"},
        relations=[
            rel(rel=r.HIERARCHY_FORWARD, url=URL.format(2)),
            rel(rel=r.HIERARCHY_REVERSE, url=URL.format(9)),
            rel(rel=r.DEPENDENCY_REVERSE, url=URL.format(3)),
            rel(rel=r.AFFECTS_FORWARD, url=URL.format(4)),
            rel(rel="Hyperlink", url="https://example.com"),
            rel(rel=None, url=URL.format(5)),
        ],
    )
    node, edges = export._parse_wi(wi)
    assert node == {"WI_ID": 1, "System.Title": "Root"}
    assert edges == [
        {"Source_ID": 1, "Target_ID": 2, "Link_Type": "Parent->Child"},
        {"Source_ID": 1, "Target_ID": 3, "Link_Type": "Successor->Predecessor"},
        {"Source_ID": 1, "Target_ID": 4, "Link_Type": "Affects"},
    ]


def test_registry_loads_without_tool_dependencies():
    code = (
        "import sys\n"
        "import workflow_automations.ado_common.relations\n"
        "heavy = {'pandas', 'networkx', 'pyarrow', 'openpyxl', 'azure',\n"
        "         'workflow_automations.ado_graph'}\n"
        "print(sorted(heavy & set(sys.modules)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
