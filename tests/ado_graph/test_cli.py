"""Offline behavior tests for the ado-graph command line."""

import os
import subprocess
import sys
import tempfile

import pytest
from ado_graph_data import edges, fake_client, nodes, run_export, wi

from workflow_automations import ado_graph as tool
from workflow_automations.ado_graph import client, export


def test_batch_size_must_be_positive(tmp_path, monkeypatch):
    client, log = fake_client({1: wi(1)}, [1])
    with pytest.raises(SystemExit):
        run_export(monkeypatch, tmp_path, client, "--batch-size", "0")
    assert log["fetched"] == []


OFFLINE_RUN = """
import logging
import sys

for name in NETWORK_MODULES:
    sys.modules[name] = None  # any import of it raises ImportError

from workflow_automations import ado_graph

assert not logging.getLogger().handlers, "importing configured root logging"
for argv in (["--help"], ["analyze", "--help"], ["export", "--help"]):
    try:
        ado_graph.main(argv)
    except SystemExit as exc:
        assert exc.code == 0, (argv, exc.code)
ado_graph.main(["analyze", "--nodes-file", sys.argv[1], "--edges-file", sys.argv[2],
                "--steps", "orphans", "--output-file", sys.argv[3]])
assert not [m for m, mod in sys.modules.items() if mod and m.startswith(("azure", "tqdm"))]
ado_graph.main(["export", "--ado-url", "https://ado.example", "--query-id", "synthetic",
                "--output-dir", sys.argv[4]])
"""


def test_analyze_and_help_run_without_network_extras(tmp_path):
    nodes().to_parquet(tmp_path / "nodes.parquet")
    edges().to_parquet(tmp_path / "edges.parquet")
    blocked = (*client.NETWORK_MODULES, "azure", "msrest", "requests_kerberos", "tqdm")
    script = f"NETWORK_MODULES = {blocked!r}\n{OFFLINE_RUN}"
    paths = ["nodes.parquet", "edges.parquet", "report.xlsx", "export"]
    run = subprocess.run(
        [sys.executable, "-c", script, *(str(tmp_path / p) for p in paths)],
        capture_output=True,
        text=True,
    )
    assert (tmp_path / "report.xlsx").exists(), run.stderr
    # Export alone fails, before any work, naming the extra to install.
    assert run.returncode == 2, run.stderr
    assert "export needs the 'ado-graph' extra" in run.stderr
    assert not (tmp_path / "export").exists()


def _export_with_real_ca(monkeypatch, tmp_path, fake, built, *extra):
    """Export with a fake client but the real CA bundle setup, recording each bundle."""

    def configure(cert_dir):
        built.append(client.configure_ca_bundle(cert_dir))
        assert os.environ["REQUESTS_CA_BUNDLE"] == built[-1]

    monkeypatch.setattr(export, "configure_ca_bundle", configure)
    monkeypatch.setattr(export, "ADOClient", fake)
    argv = ["export", "--ado-url", "https://ado.example", "--query-id", "synthetic"]
    tool.main([*argv, "--output-dir", str(tmp_path), "--formats", "parquet", *extra])


@pytest.mark.parametrize("previous", [None, "/synthetic/prior-bundle.pem"])
def test_ca_bundle_is_removed_and_setting_restored_after_failed_export(
    tmp_path, monkeypatch, previous
):
    if previous is None:
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    else:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", previous)
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "corp.pem").write_text("synthetic certificate\n")

    class Unreachable:
        def __init__(self, _url):
            pass

        def run_query_by_id(self, _project, _query):
            raise ConnectionError("synthetic ADO outage")

    built = []
    with pytest.raises(ConnectionError):
        _export_with_real_ca(
            monkeypatch, tmp_path / "out", Unreachable, built, "--cert-dir", str(certs)
        )
    assert len(built) == 1 and not os.path.exists(built[0])
    assert os.environ.get("REQUESTS_CA_BUNDLE") == previous


def test_ca_bundle_is_removed_after_successful_export(tmp_path, monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    fake, _log = fake_client({1: wi(1)}, [1])
    built = []
    _export_with_real_ca(monkeypatch, tmp_path, fake, built)
    assert (tmp_path / "nodes.parquet").exists()
    assert len(built) == 1 and not os.path.exists(built[0])
    assert "REQUESTS_CA_BUNDLE" not in os.environ


def test_failed_ca_bundle_build_leaves_no_file(tmp_path, monkeypatch):
    bundles = tmp_path / "tmp"
    bundles.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(bundles))
    certs = tmp_path / "certs"
    (certs / "unreadable.pem").mkdir(parents=True)  # read_bytes() fails on a directory
    with pytest.raises(OSError):
        client.build_merged_ca_bundle(str(certs))
    assert list(bundles.iterdir()) == []
