"""Work-item scalar, relation-target and link helpers shared by the ADO tools."""

import subprocess
import sys

import pytest

from workflow_automations.ado_common.workitem import relation_target_id, safe_int, work_item_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, 42),
        ("42", 42),
        (" 7 ", 7),
        (3.0, 3),
        (3.9, 3),
        (True, 1),
        (None, None),
        ("", None),
        ("4.0", None),
        ("abc", None),
        ([1], None),
    ],
)
def test_safe_int_converts_or_returns_none(value, expected):
    assert safe_int(value) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ado.example/Coll/_apis/wit/workItems/123", 123),
        ("https://ado.example/Coll/_apis/wit/workItems/123/", 123),
        ("123", 123),
        ("https://ado.example/Coll/_apis/wit/attachments/abc-def", None),
        ("https://example.com/page", None),
        ("https://ado.example/", None),
        ("/", None),
        ("", None),
        (None, None),
    ],
)
def test_relation_target_id_reads_the_last_url_segment(url, expected):
    assert relation_target_id(url) == expected


@pytest.mark.parametrize(
    "base_url",
    ["https://ado.example/Coll", "https://ado.example/Coll/", "https://ado.example/Coll//"],
)
def test_work_item_url_joins_without_double_slash(base_url):
    assert work_item_url(base_url, 5) == "https://ado.example/Coll/_workitems/edit/5"


def test_shared_helpers_load_without_tool_dependencies():
    """The module stays stdlib-only, so ado-triage needs no graph extras for it."""
    code = (
        "import sys\n"
        "import workflow_automations.ado_common.workitem\n"
        "heavy = {'pandas', 'networkx', 'pyarrow', 'openpyxl', 'azure',\n"
        "         'workflow_automations.ado_graph'}\n"
        "print(sorted(heavy & set(sys.modules)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
