import logging

from kodman.backend import RunOptions, _iter_log_lines, build_pod_manifest

_log = logging.getLogger("test")


def test_build_pod_manifest_sets_restart_policy_never():
    # A one-shot run pod must not be restarted on exit/failure, otherwise a
    # failed launch CrashLoopBackOffs and spams alerts.
    _, manifest, _ = build_pod_manifest(RunOptions(image="busybox"), _log)
    assert manifest["spec"]["restartPolicy"] == "Never"


def test_build_pod_manifest_basic_structure():
    _, manifest, volumes = build_pod_manifest(RunOptions(image="alpine"), _log)
    assert manifest["kind"] == "Pod"
    container = manifest["spec"]["containers"][0]
    assert container["image"] == "alpine"
    assert container["name"] == "kodman-exec"
    assert "command" not in container  # not requested
    assert volumes == []


def test_build_pod_manifest_applies_command_args_and_sa():
    _, manifest, _ = build_pod_manifest(
        RunOptions(
            image="alpine",
            command=["bash"],
            args=["-c", "echo hi"],
            service_account="runner",
        ),
        _log,
    )
    container = manifest["spec"]["containers"][0]
    assert container["command"] == ["bash"]
    assert container["args"] == ["-c", "echo hi"]
    assert manifest["spec"]["serviceAccountName"] == "runner"


class FakeStreamResponse:
    """Mimics the urllib3 response returned by read_namespaced_pod_log when
    _preload_content=False: .stream() yields arbitrary byte chunks."""

    def __init__(self, chunks):
        self._chunks = chunks

    def stream(self, amt=None, decode_content=False):
        yield from self._chunks


def test_iter_log_lines_splits_on_newlines():
    resp = FakeStreamResponse([b"line one\nline two\n"])
    assert list(_iter_log_lines(resp)) == ["line one", "line two"]


def test_iter_log_lines_reassembles_split_chunks():
    # A single logical line may be delivered across multiple chunks.
    resp = FakeStreamResponse([b"hello ", b"wor", b"ld\nnext\n"])
    assert list(_iter_log_lines(resp)) == ["hello world", "next"]


def test_iter_log_lines_yields_trailing_line_without_newline():
    resp = FakeStreamResponse([b"no trailing newline"])
    assert list(_iter_log_lines(resp)) == ["no trailing newline"]


def test_iter_log_lines_preserves_blank_lines():
    resp = FakeStreamResponse([b"a\n\nb\n"])
    assert list(_iter_log_lines(resp)) == ["a", "", "b"]


def test_iter_log_lines_handles_str_segments():
    resp = FakeStreamResponse(["text\n"])
    assert list(_iter_log_lines(resp)) == ["text"]


def test_iter_log_lines_replaces_invalid_utf8():
    resp = FakeStreamResponse([b"bad\xffbyte\n"])
    assert list(_iter_log_lines(resp)) == ["bad�byte"]
