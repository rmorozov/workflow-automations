"""Shared Kerberos session and run-scoped CA bundle, without a server or Kerberos."""

import os
import sys
import tempfile
from types import ModuleType

import pytest

pytest.importorskip("msrest")
pytest.importorskip("certifi")
import requests  # noqa: E402
from requests.adapters import HTTPAdapter  # noqa: E402

from workflow_automations.ado_common import session  # noqa: E402


@pytest.fixture
def kerberos(monkeypatch):
    """A stub requests_kerberos, so no gssapi or ticket is needed."""
    module = ModuleType("requests_kerberos")
    module.REQUIRED, module.OPTIONAL, module.DISABLED = "required", "optional", "disabled"
    module.HTTPKerberosAuth = type(
        "HTTPKerberosAuth",
        (),
        {"__init__": lambda self, **kw: setattr(self, "kw", kw), "__call__": lambda self, r: r},
    )
    monkeypatch.setitem(sys.modules, "requests_kerberos", module)
    return module


@pytest.fixture
def sent(monkeypatch):
    """Record the verify value each HTTPS request reaches the transport with."""
    calls = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        calls.append(verify)
        response = requests.Response()
        response.status_code, response.request, response.url = 200, request, request.url
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return calls


def test_kerberos_mutual_auth_defaults_to_optional_and_is_configurable(kerberos):
    signed = session.kerberos_credentials().signed_session()
    assert isinstance(signed.auth, kerberos.HTTPKerberosAuth)
    assert signed.auth.kw == {"mutual_authentication": "optional"}

    required = session.kerberos_credentials(mutual_authentication=kerberos.REQUIRED)
    assert required.signed_session().auth.kw == {"mutual_authentication": "required"}


def test_bundle_is_used_for_https_even_over_an_ambient_setting(kerberos, sent, monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/synthetic/ambient.pem")
    creds = session.kerberos_credentials(ca_bundle="/synthetic/run.pem")
    signed = creds.signed_session()
    signed.get("https://ado.example/_apis")
    signed.get("https://ado.example/_apis", verify=False)
    assert sent == ["/synthetic/run.pem", False]
    assert os.environ["REQUESTS_CA_BUNDLE"] == "/synthetic/ambient.pem"


def test_without_a_bundle_requests_keeps_its_ambient_verification(kerberos, sent, monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/synthetic/ambient.pem")
    session.kerberos_credentials().signed_session().get("https://ado.example/_apis")
    assert sent == ["/synthetic/ambient.pem"]


def test_signing_a_session_again_keeps_one_adapter(kerberos):
    creds = session.kerberos_credentials(ca_bundle="/synthetic/run.pem")
    signed = creds.signed_session()
    adapter = signed.get_adapter("https://ado.example")
    # msrest signs its session before every request.
    assert creds.signed_session(signed) is signed
    assert signed.get_adapter("https://ado.example") is adapter


def test_one_runs_bundle_never_reaches_the_next_run(kerberos, sent, monkeypatch, tmp_path):
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "corp.pem").write_text("synthetic certificate\n")

    with session.ca_bundle(str(certs)) as first:
        assert "synthetic certificate" in open(first).read()
        session.kerberos_credentials(ca_bundle=first).signed_session().get("https://a.example")
    assert not os.path.exists(first)

    # The next run, with no bundle of its own, sees requests' defaults only.
    session.kerberos_credentials().signed_session().get("https://a.example")
    assert sent[0] == first and sent[1] is True
    assert "REQUESTS_CA_BUNDLE" not in os.environ


def test_bundle_is_deleted_when_the_run_fails(tmp_path):
    with pytest.raises(RuntimeError), session.ca_bundle(None) as path:
        assert os.path.exists(path)
        raise RuntimeError("synthetic failure")
    assert not os.path.exists(path)


def test_failed_ca_bundle_build_leaves_no_file(tmp_path, monkeypatch):
    bundles = tmp_path / "tmp"
    bundles.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(bundles))
    certs = tmp_path / "certs"
    (certs / "unreadable.pem").mkdir(parents=True)  # read_bytes() fails on a directory
    with pytest.raises(OSError):
        session.build_merged_ca_bundle(str(certs))
    assert list(bundles.iterdir()) == []


def test_both_tools_connect_through_the_shared_helper(kerberos, monkeypatch):
    connection = pytest.importorskip("azure.devops.connection")
    from workflow_automations import ado_triage
    from workflow_automations.ado_graph import client

    made = []

    def fake_connection(base_url, creds):
        made.append((base_url, creds.bundle, creds.mutual_authentication))
        raise LookupError("stop after connecting")

    monkeypatch.setattr(connection, "Connection", fake_connection)
    with pytest.raises(LookupError):
        client.ADOClient("https://ado.example/", ca_bundle="/synthetic/run.pem")
    with pytest.raises(LookupError):
        ado_triage.fetch_snapshot("https://ado.example", "q")
    assert made == [
        ("https://ado.example", "/synthetic/run.pem", "optional"),
        ("https://ado.example", None, "optional"),
    ]
