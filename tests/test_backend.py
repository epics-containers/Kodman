from kodman.backend import _iter_log_lines


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
