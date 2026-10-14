"""Kerberos connection to Azure DevOps and a CA bundle scoped to one run.

Both tools connect through `connect()`. Authentication is Kerberos with the
current ticket cache. Mutual authentication defaults to OPTIONAL: IIS-hosted ADO
servers often omit the mutual-auth token on success, which fails requests-kerberos'
REQUIRED default, while OPTIONAL still verifies a token when one is sent.

A CA bundle is given to the connection, not to the process: the credentials
mount an HTTPS adapter that verifies against it on every session the SDK signs.
`REQUESTS_CA_BUNDLE` and other environment settings are left untouched, so a
bundle built for one run can never be picked up by the next. Without a bundle
the connection uses requests' normal verification, ambient settings included.

The SDK, msrest, requests, requests-kerberos and certifi are imported only when a
connection or bundle is built, so the offline modes need none of them.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def build_merged_ca_bundle(cert_dir: str | None) -> str:
    """Write certifi's bundle plus every *.pem/*.crt under *cert_dir* to a temp file.

    Files are appended recursively, sorted per extension. A *cert_dir* that is not
    a directory warns and leaves certifi alone. Returns the path; the caller
    deletes it. A failed build deletes its partial file.
    """
    import certifi

    tmp = tempfile.NamedTemporaryFile(prefix="ado_ca_bundle_", suffix=".pem", delete=False)
    tmp.close()
    try:
        shutil.copyfile(certifi.where(), tmp.name)
        log.info("Merged CA bundle initialised from certifi (%s).", certifi.where())

        if cert_dir:
            cert_path = Path(cert_dir)
            if not cert_path.is_dir():
                log.warning("--cert-dir '%s' is not a directory – skipping custom certs.", cert_dir)
            else:
                added = 0
                for pattern in ("**/*.pem", "**/*.crt"):
                    for cert_file in sorted(cert_path.glob(pattern)):
                        with open(tmp.name, "ab") as fh:
                            fh.write(b"\n")
                            fh.write(cert_file.read_bytes())
                        log.debug("  + appended cert: %s", cert_file)
                        added += 1
                log.info("Appended %d custom certificate file(s) from '%s'.", added, cert_dir)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    return tmp.name


@contextmanager
def ca_bundle(cert_dir: str | None) -> Iterator[str]:
    """Build a merged CA bundle for the block and delete it afterwards, also on failure."""
    path = build_merged_ca_bundle(cert_dir)
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


def kerberos_credentials(mutual_authentication: Any = None, ca_bundle: str | None = None) -> Any:
    """msrest credentials that sign each session with Kerberos and, if given, *ca_bundle*.

    *mutual_authentication* is a requests_kerberos constant; None means OPTIONAL.
    Raises ImportError when msrest, requests or requests-kerberos is missing.
    """
    from msrest.authentication import Authentication
    from requests.adapters import HTTPAdapter
    from requests_kerberos import OPTIONAL, HTTPKerberosAuth

    mutual = OPTIONAL if mutual_authentication is None else mutual_authentication

    class BundleAdapter(HTTPAdapter):
        """Verifies HTTPS against the run's bundle, whatever requests resolved."""

        def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
            # requests turns verify=True into an ambient REQUESTS_CA_BUNDLE path, if
            # set; only an explicit verify=False is kept.
            verify = ca_bundle if verify is not False else False
            return super().send(
                request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies
            )

    class KerberosCredentials(Authentication):
        mutual_authentication = mutual
        bundle = ca_bundle

        def signed_session(self, session=None):
            session = super().signed_session(session)
            session.auth = HTTPKerberosAuth(mutual_authentication=mutual)
            # msrest signs the same session before every request; mount once.
            if ca_bundle and not isinstance(session.adapters.get("https://"), BundleAdapter):
                session.mount("https://", BundleAdapter())
            return session

    return KerberosCredentials()


def connect(
    base_url: str, *, mutual_authentication: Any = None, ca_bundle: str | None = None
) -> Any:
    """An azure-devops Connection authenticated by `kerberos_credentials()`."""
    from azure.devops.connection import Connection

    creds = kerberos_credentials(mutual_authentication, ca_bundle)
    return Connection(base_url=base_url, creds=creds)
