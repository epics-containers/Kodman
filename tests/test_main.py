import argparse
import logging
import signal

import pytest

from kodman.__main__ import RunInterruptedError, engine

# `@engine.add_command` registers an instance and rebinds the class name to
# None, so reach the Run command class via the engine's registry.
RunCommand = next(type(c) for c in engine._commands if type(c).__name__ == "Run")

_log = logging.getLogger("test")


class FakeBackend:
    """Minimal stand-in for Backend that records delete() calls."""

    def __init__(self, raise_on_run=None):
        self.return_code = 0
        self.pod_name = ""
        self._raise_on_run = raise_on_run
        self.deleted: list[str] = []
        self.swept: list[int] = []

    def connect(self):
        pass

    def sweep(self, options):
        self.swept.append(options.ttl_seconds)
        return []

    def run(self, options):
        # Mirror the real backend: the name is known before any failure.
        self.pod_name = "kodman-run-123"
        if self._raise_on_run:
            raise self._raise_on_run
        return self.pod_name

    def delete(self, options):
        self.deleted.append(options.name)


def _args(rm, extra=()):
    """Parse a real `kodman run` command line.

    Built from the Run command's own parser rather than by hand, so a new
    argument cannot leave these tests passing a Namespace that Run.do then
    trips over (as --cpus did).
    """
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cli_command")
    RunCommand().add(subparsers)

    argv = ["run"]
    if rm:
        argv.append("--rm")
    argv += [*extra, "busybox"]
    return parser.parse_args(argv)


_ENV = {"KODMAN_SERVICE_ACCOUNT": "", "KODMAN_POD_TTL": None}


def test_rm_deletes_pod_on_success():
    ctx = FakeBackend()
    RunCommand().do(_args(rm=True), ctx, _ENV, _log)
    assert ctx.deleted == ["kodman-run-123"]


def test_rm_deletes_pod_when_run_raises():
    # The boot-loop fix: a failed launch must still be cleaned up under --rm.
    ctx = FakeBackend(raise_on_run=RuntimeError("launch failed"))
    with pytest.raises(RuntimeError):
        RunCommand().do(_args(rm=True), ctx, _ENV, _log)
    assert ctx.deleted == ["kodman-run-123"]


def test_no_rm_keeps_pod():
    ctx = FakeBackend()
    RunCommand().do(_args(rm=False), ctx, _ENV, _log)
    assert ctx.deleted == []


def test_args_reach_the_backend():
    # Guards the wiring between the parser and RunOptions.
    class Recorder(FakeBackend):
        def run(self, options):
            self.options = options
            return super().run(options)

    ctx = Recorder()
    RunCommand().do(_args(rm=True, extra=["--cpus", "4"]), ctx, _ENV, _log)
    assert ctx.options.image == "busybox"
    assert ctx.options.cpus == "4"


def test_interrupted_run_removes_pod_without_rm():
    # A pod left running by a killed client keeps burning CPU, and k8s has no
    # way to stop one short of deleting it.
    ctx = FakeBackend(raise_on_run=RunInterruptedError(signal.SIGTERM))
    command = RunCommand()
    command.do(_args(rm=False), ctx, _ENV, _log)
    assert ctx.deleted == ["kodman-run-123"]
    assert command.exit_code == 128 + signal.SIGTERM


def test_signal_handlers_are_restored():
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    ctx = FakeBackend()
    RunCommand().do(_args(rm=True), ctx, _ENV, _log)
    assert {sig: signal.getsignal(sig) for sig in original} == original


def test_signal_handlers_are_restored_after_failure():
    original = signal.getsignal(signal.SIGTERM)
    ctx = FakeBackend(raise_on_run=RuntimeError("launch failed"))
    with pytest.raises(RuntimeError):
        RunCommand().do(_args(rm=True), ctx, _ENV, _log)
    assert signal.getsignal(signal.SIGTERM) is original


def test_run_sweeps_with_default_ttl():
    ctx = FakeBackend()
    RunCommand().do(_args(rm=True), ctx, _ENV, _log)
    assert ctx.swept == [3600]


def test_pod_ttl_env_overrides_the_default():
    ctx = FakeBackend()
    env = {**_ENV, "KODMAN_POD_TTL": 0}
    RunCommand().do(_args(rm=True), ctx, env, _log)
    assert ctx.swept == [0]
