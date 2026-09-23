"""get_incluster_context names the cluster and service account it runs as."""

import base64
import json
from pathlib import Path

import pytest

from kodman.backend import get_incluster_context


def _jwt(claims: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


@pytest.fixture
def sa_dir(tmp_path: Path) -> Path:
    (tmp_path / "namespace").write_text("ci\n")
    return tmp_path


def test_context_names_cluster_and_service_account(sa_dir, monkeypatch):
    (sa_dir / "token").write_text(_jwt({"sub": "system:serviceaccount:ci:kodman"}))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    assert get_incluster_context(sa_dir) == {
        "namespace": "ci",
        "cluster": "10.43.0.1:443",
        "user": "system:serviceaccount:ci:kodman",
    }


def test_context_without_token_or_service_env(sa_dir, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    context = get_incluster_context(sa_dir)
    assert context["namespace"] == "ci"
    assert context["cluster"] == "in-cluster"
    assert context["user"] == "in-cluster service account"


@pytest.mark.parametrize("token", ["not-a-jwt", "a.!!!.c", _jwt({"iss": "x"})])
def test_unreadable_token_falls_back(sa_dir, token):
    (sa_dir / "token").write_text(token)
    assert get_incluster_context(sa_dir)["user"] == "in-cluster service account"
