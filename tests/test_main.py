import argparse
import logging

import pytest

from kodman.__main__ import engine

# `@engine.add_command` registers an instance and rebinds the class name to
# None, so reach the Run command class via the engine's registry.
RunCommand = next(type(c) for c in engine._commands if type(c).__name__ == "Run")

_log = logging.getLogger("test")


class FakeBackend:
    """Minimal stand-in for Backend that records delete() calls."""

    def __init__(self, raise_on_run=False):
        self.return_code = 0
        self.pod_name = ""
        self._raise_on_run = raise_on_run
        self.deleted: list[str] = []

    def connect(self):
        pass

    def run(self, options):
        # Mirror the real backend: the name is known before any failure.
        self.pod_name = "kodman-run-123"
        if self._raise_on_run:
            raise RuntimeError("launch failed")
        return self.pod_name

    def delete(self, options):
        self.deleted.append(options.name)


def _args(rm):
    return argparse.Namespace(
        entrypoint=None,
        rm=rm,
        volume=None,
        image="busybox",
        command=None,
        args=[],
    )


_ENV = {"KODMAN_SERVICE_ACCOUNT": ""}


def test_rm_deletes_pod_on_success():
    ctx = FakeBackend()
    RunCommand().do(_args(rm=True), ctx, _ENV, _log)
    assert ctx.deleted == ["kodman-run-123"]


def test_rm_deletes_pod_when_run_raises():
    # The boot-loop fix: a failed launch must still be cleaned up under --rm.
    ctx = FakeBackend(raise_on_run=True)
    with pytest.raises(RuntimeError):
        RunCommand().do(_args(rm=True), ctx, _ENV, _log)
    assert ctx.deleted == ["kodman-run-123"]


def test_no_rm_keeps_pod():
    ctx = FakeBackend()
    RunCommand().do(_args(rm=False), ctx, _ENV, _log)
    assert ctx.deleted == []
