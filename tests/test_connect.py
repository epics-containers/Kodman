"""Backend.connect picks the kubeconfig context and namespace it is told to."""

import logging
from pathlib import Path

import pytest
from kubernetes.config import kube_config
from kubernetes.config.config_exception import ConfigException

from kodman.backend import Backend

KUBECONFIG = """
apiVersion: v1
kind: Config
current-context: dev
clusters:
- name: dev-cluster
  cluster: {server: "https://dev.example:6443"}
- name: prod-cluster
  cluster: {server: "https://prod.example:6443"}
users:
- name: alice
  user: {token: abc}
contexts:
- name: dev
  context: {cluster: dev-cluster, user: alice, namespace: dev-ns}
- name: prod
  context: {cluster: prod-cluster, user: alice}
"""


@pytest.fixture
def kubeconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    path.write_text(KUBECONFIG)
    monkeypatch.setattr(kube_config, "KUBE_CONFIG_DEFAULT_LOCATION", str(path))
    return path


def _connect(**kwargs) -> Backend:
    backend = Backend(logging.getLogger("test"))
    backend.connect(**kwargs)
    return backend


def test_current_context_is_used_by_default(kubeconfig: Path):
    backend = _connect()
    assert backend._context["cluster"] == "dev-cluster"
    assert backend._context["namespace"] == "dev-ns"
    assert backend._client.api_client.configuration.host == "https://dev.example:6443"


def test_context_override_selects_another_cluster(kubeconfig: Path):
    backend = _connect(context="prod")
    assert backend._context["cluster"] == "prod-cluster"
    assert backend._client.api_client.configuration.host == "https://prod.example:6443"


def test_context_without_namespace_uses_default(kubeconfig: Path):
    # As kubectl does, rather than failing with a KeyError at run time.
    assert _connect(context="prod")._context["namespace"] == "default"


def test_namespace_override(kubeconfig: Path):
    assert _connect(namespace="other")._context["namespace"] == "other"


def test_unknown_context_is_an_error(kubeconfig: Path):
    # Must not fall back to in-cluster config and run somewhere unexpected.
    with pytest.raises(ConfigException):
        _connect(context="missing")
