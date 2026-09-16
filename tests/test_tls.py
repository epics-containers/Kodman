"""relax_x509_strict must accept Kubernetes-style CAs and keep verification on."""

import datetime
import ipaddress
import json
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from kubernetes import client
from kubernetes.client.models.v1_pod_list import V1PodList

from kodman.backend import relax_x509_strict


def make_ca(name: str):
    """A root CA without key identifiers, like old kubeadm and EKS clusters."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_server_cert(ca_key, ca_cert):
    """A server certificate for 127.0.0.1 without an Authority Key Identifier."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kube")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def write_pem(path: Path, *items) -> Path:
    data = b""
    for item in items:
        if isinstance(item, x509.Certificate):
            data += item.public_bytes(serialization.Encoding.PEM)
        else:
            data += item.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
    path.write_bytes(data)
    return path


class PodListHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"kind": "PodList", "apiVersion": "v1", "items": []})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def api_server(tmp_path: Path):
    """An HTTPS server whose certificate chain has no key identifiers."""
    ca_key, ca_cert = make_ca("kubernetes")
    server_key, server_cert = make_server_cert(ca_key, ca_cert)
    ca_pem = write_pem(tmp_path / "ca.pem", ca_cert)
    server_pem = write_pem(tmp_path / "server.pem", server_cert, server_key)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), PodListHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(server_pem)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{httpd.server_address[1]}", ca_pem
    httpd.shutdown()
    httpd.server_close()


def make_api(host: str, ca_pem: Path, verify_ssl: bool = True) -> client.CoreV1Api:
    configuration = client.Configuration(host=host)
    configuration.ssl_ca_cert = str(ca_pem)
    configuration.verify_ssl = verify_ssl
    configuration.retries = 0  # pyright: ignore[reportAttributeAccessIssue]
    return client.CoreV1Api(client.ApiClient(configuration))


@pytest.mark.skipif(
    sys.version_info < (3, 13), reason="VERIFY_X509_STRICT is a default from 3.13"
)
def test_strict_default_rejects_ca_without_key_identifier(api_server):
    host, ca_pem = api_server
    api = make_api(host, ca_pem)
    with pytest.raises(Exception, match="Authority Key Identifier"):
        api.list_namespaced_pod("default")


def test_relaxed_client_accepts_ca_without_key_identifier(api_server):
    host, ca_pem = api_server
    api = make_api(host, ca_pem)
    relax_x509_strict(api.api_client)
    pods = cast(V1PodList, api.list_namespaced_pod("default"))
    assert pods.items == []


def test_relaxed_client_still_rejects_untrusted_ca(api_server, tmp_path: Path):
    host, _ = api_server
    _, other_ca = make_ca("not the cluster CA")
    api = make_api(host, write_pem(tmp_path / "other.pem", other_ca))
    relax_x509_strict(api.api_client)
    with pytest.raises(Exception, match="CERTIFICATE_VERIFY_FAILED"):
        api.list_namespaced_pod("default")


def test_verify_ssl_disabled_is_left_unchanged(api_server):
    host, ca_pem = api_server
    api = make_api(host, ca_pem, verify_ssl=False)
    relax_x509_strict(api.api_client)
    pool_kw = api.api_client.rest_client.pool_manager.connection_pool_kw
    assert "ssl_context" not in pool_kw
