"""The shared raw snapshot: file format, validation and safe writes."""

import json
from datetime import datetime

import pytest

from workflow_automations.ado_common.snapshot import (
    SCHEMA,
    VERSION,
    RawSnapshot,
    SnapshotError,
    load_snapshot,
    save_snapshot,
)


def raw(**overrides):
    values = {
        "producer": "ado-graph",
        "captured_at": "2026-10-14T00:00:00+00:00",
        "source": {"base_url": "https://ado.example", "project": "", "query_id": "q"},
        "roots": [1],
        "items": [
            {
                "id": 1,
                "fields": {
                    "System.Title": "Zürich – 東京",
                    "System.AssignedTo": {"displayName": "A"},
                },
                "relations": [{"rel": "System.LinkTypes.Hierarchy-Forward", "url": "u/2"}],
            }
        ],
        "linked": {2: {"System.Title": "Linked"}},
        "omitted": [3],
        "warnings": ["w"],
    }
    values.update(overrides)
    return RawSnapshot(**values)


def doc(**overrides):
    data = raw().to_json()
    data.update(overrides)
    return data


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "snap.json"
    original = raw(filters={"skip_types": ["task"]}, options={"filtered_relations": "follow"})
    save_snapshot(original, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data["schema"], data["version"]) == (SCHEMA, VERSION)
    assert data["linked"] == {"2": {"System.Title": "Linked"}}
    assert load_snapshot(path) == original


def test_filters_decide_completeness():
    assert not raw().is_filtered
    assert not raw(filters={"skip_types": [], "include_projects": []}).is_filtered
    # A relation policy is an option, not a filter.
    assert not raw(options={"filtered_relations": "stop"}).is_filtered
    assert raw(filters={"include_projects": ["example"]}).is_filtered


def test_existing_file_needs_overwrite_and_failed_write_keeps_it(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text("previous", encoding="utf-8")
    with pytest.raises(FileExistsError):
        save_snapshot(raw(), path)
    bad = raw(items=[{"id": 1, "fields": {"date": datetime(2026, 1, 1)}, "relations": []}])
    with pytest.raises(SnapshotError, match="not JSON data"):
        save_snapshot(bad, path, overwrite=True)
    assert path.read_text(encoding="utf-8") == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]
    save_snapshot(raw(), path, overwrite=True)
    assert load_snapshot(path) == raw()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (doc(schema="other"), "not an ADO snapshot"),
        (doc(version=VERSION + 1), "unsupported snapshot version"),
        (doc(version="1"), "version must be an integer"),
        (doc(roots=[True]), "integer work-item ID"),
        (doc(items=[{"id": 1}, {"id": 1}]), "appears twice"),
        (doc(items=[{"id": 1, "relations": [{"rel": None}]}]), "rel must be a string"),
        (doc(linked={"²": {}}), "not a work-item ID"),
        (doc(linked={"09": {}}), "not a work-item ID"),
        (doc(roots=[1, 1]), "appears twice in roots"),
        (doc(filters={"skip_types": "task"}), "filters.skip_types must be a list"),
        (doc(producer=None), "producer must be a string"),
        ([], "snapshot must be an object"),
    ],
)
def test_invalid_documents_are_refused(data, message):
    with pytest.raises(SnapshotError, match=message):
        RawSnapshot.from_json(data)


def test_non_json_file_is_refused(tmp_path):
    path = tmp_path / "snap.json"
    path.write_bytes(b"\xff not json")
    with pytest.raises(SnapshotError, match="not a JSON snapshot"):
        load_snapshot(path)
    path.write_text(json.dumps(doc()).replace('"Linked"', "NaN"), encoding="utf-8")
    with pytest.raises(SnapshotError, match="NaN is not JSON data"):
        load_snapshot(path)


@pytest.mark.parametrize(
    "bad",
    [
        raw(roots=[True]),
        raw(items=[{"id": 1, "fields": {}, "relations": [{"rel": None, "url": None}]}]),
        raw(items=[{"id": 1, "fields": {}, "relations": []}] * 2),
    ],
)
def test_save_refuses_what_load_would_refuse(tmp_path, bad):
    path = tmp_path / "snap.json"
    with pytest.raises(SnapshotError):
        save_snapshot(bad, path)
    assert not path.exists()
