"""Offline behavior tests for the ado-graph command line."""

import os
import re
import subprocess
import sys
from contextlib import nullcontext

import pandas as pd
import pytest
from ado_graph_data import edges, fake_client, nodes, run_export, wi

from workflow_automations import ado_graph as tool
from workflow_automations.ado_graph import cli, client, export


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
    """Export with a fake client but the real CA bundle, recording what each client got."""

    class Recording(fake):
        def __init__(self, url, ca_bundle=None):
            built.append(ca_bundle)
            # The bundle exists, holds the custom certs, and is not in the environment.
            assert "synthetic certificate" in open(ca_bundle).read() or "--cert-dir" not in extra
            assert os.environ.get("REQUESTS_CA_BUNDLE") != ca_bundle
            super().__init__(url, ca_bundle=ca_bundle)

    monkeypatch.setattr(export, "ADOClient", Recording)
    argv = ["export", "--ado-url", "https://ado.example", "--query-id", "synthetic"]
    tool.main([*argv, "--output-dir", str(tmp_path), "--formats", "parquet", *extra])


@pytest.mark.parametrize("previous", [None, "/synthetic/prior-bundle.pem"])
def test_ca_bundle_is_removed_and_setting_untouched_after_failed_export(
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
        def __init__(self, _url, ca_bundle=None):
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


def test_each_export_gets_its_own_bundle_and_none_outlives_it(tmp_path, monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    fake, _log = fake_client({1: wi(1)}, [1])
    built = []
    _export_with_real_ca(monkeypatch, tmp_path / "a", fake, built)
    _export_with_real_ca(monkeypatch, tmp_path / "b", fake, built)
    assert (tmp_path / "b" / "nodes.parquet").exists()
    assert len(set(built)) == 2 and not any(os.path.exists(path) for path in built)
    assert "REQUESTS_CA_BUNDLE" not in os.environ


def _rejected(capsys, argv):
    """Run main expecting an argparse error; return its message."""
    with pytest.raises(SystemExit) as exc:
        tool.main(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize(
    ("formats", "message"),
    [
        ("parquet,xlsx", "--formats: unknown xlsx; choose from parquet, csv, excel"),
        (" , ", "--formats: no format given"),
    ],
)
def test_export_rejects_bad_formats_before_any_service_call(
    tmp_path, monkeypatch, capsys, formats, message
):
    fake, log = fake_client({1: wi(1)}, [1])
    monkeypatch.setattr(export, "ADOClient", fake)
    monkeypatch.setattr(cli, "import_network_modules", lambda: pytest.fail("imported"))
    out = tmp_path / "out"
    argv = ["export", "--ado-url", "https://ado.example", "--query-id", "q"]
    err = _rejected(capsys, [*argv, "--output-dir", str(out), "--formats", formats])
    assert message in err
    assert log["fetched"] == [] and not out.exists()


def test_replay_rejects_unknown_formats_before_reading_the_snapshot(tmp_path, capsys):
    out = tmp_path / "out"
    argv = ["replay", "--snapshot", str(tmp_path / "absent.json"), "--output-dir", str(out)]
    assert "--formats: unknown pdf" in _rejected(capsys, [*argv, "--formats", "csv,pdf"])
    assert not out.exists()


def test_formats_are_normalized_and_deduplicated(tmp_path, monkeypatch):
    fake, _log = fake_client({1: wi(1)}, [1])
    monkeypatch.setattr(export, "ADOClient", fake)
    monkeypatch.setattr(export, "ca_bundle", lambda _dir: nullcontext())
    argv = ["export", "--ado-url", "https://ado.example", "--query-id", "q"]
    tool.main([*argv, "--output-dir", str(tmp_path), "--formats", " CSV,,csv ,Parquet,"])
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "edges.csv",
        "edges.parquet",
        "nodes.csv",
        "nodes.parquet",
    ]


def _analyze(tmp_path, *extra):
    argv = ["analyze", "--nodes-file", str(tmp_path / "nodes.parquet")]
    argv += ["--edges-file", str(tmp_path / "edges.parquet")]
    return [*argv, "--output-file", str(tmp_path / "out" / "report.xlsx"), *extra]


@pytest.fixture
def parquet_inputs(tmp_path):
    nodes().to_parquet(tmp_path / "nodes.parquet")
    edges().to_parquet(tmp_path / "edges.parquet")
    return tmp_path


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ("orphans,cycle", "--steps: unknown cycle;"),
        (
            "cycles@deps,all@dependency,@hierarchy",
            "unknown cycles@deps, all@dependency, @hierarchy",
        ),
        (", ,", "--steps: no step given"),
    ],
)
def test_analyze_rejects_bad_steps_before_reading_inputs(
    parquet_inputs, monkeypatch, capsys, steps, message
):
    monkeypatch.setattr(cli, "run_analyze", lambda _args: pytest.fail("analysis ran"))
    err = _rejected(capsys, _analyze(parquet_inputs, "--steps", steps))
    assert message in err
    assert "cycles, orphans" in err and "hierarchy, dependency" in err
    assert not (parquet_inputs / "out").exists()


@pytest.mark.parametrize("steps", ["all", "ALL , cycles@Dependency,", "cycles@,metrics@all"])
def test_analyze_accepts_valid_steps(parquet_inputs, monkeypatch, steps):
    ran = []
    monkeypatch.setattr(cli, "run_analyze", ran.append)
    tool.main(_analyze(parquet_inputs, "--steps", steps))
    assert len(ran) == 1


@pytest.mark.parametrize(
    ("which", "frame", "message"),
    [
        ("nodes", lambda: nodes().drop(columns="WI_ID"), "--nodes-file .* lacks .* WI_ID"),
        (
            "edges",
            lambda: edges().drop(columns=["Target_ID", "Link_Type"]),
            "--edges-file .* lacks .* Target_ID, Link_Type",
        ),
    ],
)
def test_analyze_rejects_inputs_missing_required_columns(
    parquet_inputs, monkeypatch, capsys, which, frame, message
):
    frame().to_parquet(parquet_inputs / f"{which}.parquet")
    monkeypatch.setattr(cli, "run_analyze", lambda _args: pytest.fail("analysis ran"))
    assert re.search(message, _rejected(capsys, _analyze(parquet_inputs)))
    assert not (parquet_inputs / "out").exists()


@pytest.mark.parametrize("make", ["missing", "csv", "directory"])
def test_analyze_rejects_unreadable_parquet(parquet_inputs, monkeypatch, capsys, make):
    path = parquet_inputs / "edges.parquet"
    path.unlink()
    if make == "csv":
        edges().to_csv(path, index=False)
    elif make == "directory":
        path.mkdir()
    monkeypatch.setattr(cli, "run_analyze", lambda _args: pytest.fail("analysis ran"))
    err = _rejected(capsys, _analyze(parquet_inputs))
    assert f"--edges-file {path} is not a readable Parquet file" in err
    assert not (parquet_inputs / "out").exists()


def test_all_unknown_steps_no_longer_reach_an_empty_workbook(parquet_inputs, capsys):
    err = _rejected(capsys, _analyze(parquet_inputs, "--steps", "bogus"))
    assert "--steps: unknown bogus" in err
    assert not (parquet_inputs / "out").exists()


def test_analyze_budget_options_reach_the_report(tmp_path):
    # Two 2-cycles and a 3-cycle among 1..4 (`Blocks` both ways is a 2-cycle).
    pairs = [(1, 2), (2, 1), (3, 4), (4, 3), (1, 3), (3, 2)]
    pd.DataFrame(pairs, columns=["Source_ID", "Target_ID"]).assign(Link_Type="Blocks").to_parquet(
        tmp_path / "edges.parquet"
    )
    nodes().to_parquet(tmp_path / "nodes.parquet")
    budgets = ["--max-cycles", "1", "--max-cycle-rows", "50", "--centrality-sources", "2"]
    tool.main(_analyze(tmp_path, "--steps", "cycles,metrics", *budgets))
    sheets = pd.read_excel(tmp_path / "out" / "report.xlsx", sheet_name=None)
    assert sheets["cycles"]["Cycle_Index"].nunique() == 1
    assert set(sheets["cycles"]["Cycles_Complete"]) == {False}
    assert set(sheets["metrics"]["Betweenness_Sources"]) == {2}


@pytest.mark.parametrize("option", ["--max-cycles", "--max-cycle-rows", "--centrality-sources"])
def test_analyze_budgets_must_be_positive(parquet_inputs, capsys, option):
    assert "must be at least 1" in _rejected(capsys, _analyze(parquet_inputs, option, "0"))


def test_meta_fields_reach_the_report_and_config(tmp_path):
    nodes().assign(**{"Custom.Team": ["Red", "Blue", "Blue", "Red"]}).to_parquet(
        tmp_path / "nodes.parquet"
    )
    edges().to_parquet(tmp_path / "edges.parquet")
    meta = ["--meta-field", "Team=Custom.Team", "--meta-field", "Sprint=Org.Sprint"]
    tool.main(_analyze(tmp_path, "--steps", "metrics", *meta, "--missing-meta", "blank"))
    sheets = pd.read_excel(tmp_path / "out" / "report.xlsx", sheet_name=None)
    assert sheets["metrics"].set_index("WI_ID").loc[2, "Team"] == "Blue"
    assert sheets["metrics"]["Sprint"].isna().all()
    config = sheets["Config"].set_index("Setting")["Value"]
    assert config["meta_fields"] == "Team=Custom.Team, Sprint=Org.Sprint"
    assert config["missing_meta"] == "blank"
    about = sheets["About"].set_index("Item")["Value"]
    assert about["Metadata fields not in the nodes file"] == "Org.Sprint"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--meta-field", "Title=Custom.Team"], "Title is a column the report writes"),
        (["--meta-field", "Effort=Custom.Team"], "Effort is already a column of the nodes file"),
        (
            ["--meta-preset", "legacy", "--missing-meta", "error"],
            "field KL.Roadmap_State is not in the nodes file",
        ),
        (["--meta-preset", "acme"], "invalid choice: 'acme'"),
    ],
)
def test_analyze_rejects_bad_meta_fields_before_analysis(
    parquet_inputs, monkeypatch, capsys, extra, message
):
    monkeypatch.setattr(cli, "run_analyze", lambda _args: pytest.fail("analysis ran"))
    assert message in _rejected(capsys, _analyze(parquet_inputs, *extra))
    assert not (parquet_inputs / "out").exists()
