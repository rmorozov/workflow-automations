"""Pure saved-query ID extraction and chunking shared by the ADO tools."""

from types import SimpleNamespace

import pytest

from workflow_automations.ado_common.query import (
    ADO_BATCH_LIMIT,
    chunks,
    flat_result_ids,
    link_result_ids,
    query_result_ids,
)


def ref(wi_id):
    return SimpleNamespace(id=wi_id)


def link(source, target):
    return SimpleNamespace(
        source=None if source is None else ref(source),
        target=None if target is None else ref(target),
    )


def result(items=None, links=None):
    return SimpleNamespace(work_items=items, work_item_relations=links)


def test_missing_lists_give_no_ids():
    for empty in (result(), result([], [])):
        assert flat_result_ids(empty) == []
        assert link_result_ids(empty) == []
        assert query_result_ids(empty) == []


def test_flat_ids_keep_query_order():
    assert flat_result_ids(result([ref(3), ref(1)])) == [3, 1]


def test_link_ids_skip_missing_ends_and_repeat_endpoints():
    links = [link(None, 5), link(5, 2), link(2, 5), link(5, None)]
    assert link_result_ids(result(links=links)) == [5, 2]


def test_query_ids_union_flat_items_before_link_endpoints():
    both = result([ref(4), ref(2)], [link(None, 3), link(3, 2)])
    assert query_result_ids(both) == [4, 2, 3]


def test_chunks_split_at_the_batch_limit_by_default():
    sizes = [len(c) for c in chunks(range(1, 2 * ADO_BATCH_LIMIT + 2))]
    assert ADO_BATCH_LIMIT == 200
    assert sizes == [200, 200, 1]


def test_chunks_yield_lists_in_order_and_nothing_for_no_ids():
    assert list(chunks((1, 2, 3), 2)) == [[1, 2], [3]]
    assert list(chunks([], 5)) == []


@pytest.mark.parametrize("size", [0, -1])
def test_chunks_reject_a_size_below_one(size):
    with pytest.raises(ValueError, match="at least 1"):
        list(chunks([1], size))
