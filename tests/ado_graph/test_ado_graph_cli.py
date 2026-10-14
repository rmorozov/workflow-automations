"""Offline behavior tests for the ado-graph command line."""

import pytest
from ado_graph_data import fake_client, run_export, wi


def test_batch_size_must_be_positive(tmp_path, monkeypatch):
    client, log = fake_client({1: wi(1)}, [1])
    with pytest.raises(SystemExit):
        run_export(monkeypatch, tmp_path, client, "--batch-size", "0")
    assert log["fetched"] == []
