import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kubernetes.client.models.v1_object_meta import V1ObjectMeta
from kubernetes.client.models.v1_pod import V1Pod
from kubernetes.client.models.v1_pod_list import V1PodList
from kubernetes.client.models.v1_pod_status import V1PodStatus
from kubernetes.client.rest import ApiException

from kodman.backend import (
    MANAGED_BY_LABEL,
    MANAGED_BY_SELECTOR,
    MANAGED_BY_VALUE,
    Backend,
    DeleteOptions,
    RunOptions,
    SweepOptions,
    _iter_log_lines,
    build_pod_manifest,
)

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


def test_build_pod_manifest_sets_cpu_when_asked():
    _, manifest, _ = build_pod_manifest(RunOptions(image="alpine", cpus="4"), _log)
    resources = manifest["spec"]["containers"][0]["resources"]
    # requested as well as limited: a request left to a namespace default can
    # be a fraction of the limit, and the container is then throttled
    assert resources == {"requests": {"cpu": "4"}, "limits": {"cpu": "4"}}


def test_build_pod_manifest_leaves_cpu_to_the_cluster_by_default():
    _, manifest, _ = build_pod_manifest(RunOptions(image="alpine"), _log)
    assert "resources" not in manifest["spec"]["containers"][0]


def test_build_pod_manifest_labels_the_pod_for_sweeping():
    # Unlabelled pods cannot be found again, so cannot be reaped.
    _, manifest, _ = build_pod_manifest(RunOptions(image="busybox"), _log)
    assert manifest["metadata"]["labels"][MANAGED_BY_LABEL] == MANAGED_BY_VALUE


def _pod(name, phase, age_seconds):
    created = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return V1Pod(
        metadata=V1ObjectMeta(name=name, creation_timestamp=created),
        status=V1PodStatus(phase=phase),
    )


class FakePodApi:
    """Stands in for CoreV1Api over the two calls sweep() makes."""

    def __init__(self, pods, list_error=None, delete_error=None):
        self._pods = pods
        self._list_error = list_error
        self._delete_error = delete_error
        self.deleted: list[str] = []
        self.selectors: list[str] = []

    def list_namespaced_pod(self, namespace, label_selector=""):
        if self._list_error:
            raise self._list_error
        self.selectors.append(label_selector)
        return V1PodList(items=self._pods)

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds=None):
        if self._delete_error:
            raise self._delete_error
        self.deleted.append(name)


def _backend(api):
    backend = Backend(_log)
    backend._client = api
    backend._context = {"namespace": "test-ns"}
    return backend


def test_sweep_removes_finished_pods_past_their_ttl():
    api = FakePodApi([_pod("old-failed", "Failed", 7200)])
    assert _backend(api).sweep(SweepOptions(ttl_seconds=3600)) == ["old-failed"]
    assert api.deleted == ["old-failed"]
    # Only ever kodman's own pods
    assert api.selectors == [MANAGED_BY_SELECTOR]


def test_sweep_keeps_finished_pods_within_their_ttl():
    # The post-mortem window: a run that just failed is still inspectable.
    api = FakePodApi([_pod("fresh-failed", "Failed", 60)])
    assert _backend(api).sweep(SweepOptions(ttl_seconds=3600)) == []
    assert api.deleted == []


def test_sweep_never_touches_live_pods():
    # These may belong to a kodman running right now, however old they are.
    api = FakePodApi(
        [_pod("running", "Running", 99999), _pod("pending", "Pending", 99999)]
    )
    assert _backend(api).sweep(SweepOptions(ttl_seconds=0)) == []
    assert api.deleted == []


def test_sweep_collects_succeeded_as_well_as_failed():
    api = FakePodApi([_pod("done", "Succeeded", 7200), _pod("bad", "Failed", 7200)])
    assert _backend(api).sweep(SweepOptions(ttl_seconds=3600)) == ["done", "bad"]


def test_sweep_disabled_by_negative_ttl():
    api = FakePodApi([_pod("old-failed", "Failed", 7200)])
    assert _backend(api).sweep(SweepOptions(ttl_seconds=-1)) == []
    assert api.selectors == []  # Not even listed


def test_sweep_survives_a_namespace_it_cannot_list():
    # Missing RBAC must warn and let the run proceed, not abort it.
    api = FakePodApi([], list_error=ApiException(status=403))
    assert _backend(api).sweep(SweepOptions(ttl_seconds=0)) == []


def test_sweep_survives_an_unexpected_response_type():
    # A client whose response type moves under us must not take the run down.
    class OddApi:
        def list_namespaced_pod(self, namespace, label_selector=""):
            return {"items": []}  # Not the V1PodList the stubs promise

    assert _backend(OddApi()).sweep(SweepOptions(ttl_seconds=0)) == []


def test_sweep_survives_a_failed_delete():
    api = FakePodApi(
        [_pod("old-failed", "Failed", 7200)], delete_error=ApiException(status=404)
    )
    assert _backend(api).sweep(SweepOptions(ttl_seconds=0)) == []


class FakeDeleteApi:
    """Stands in for CoreV1Api over the calls delete() makes.

    ``reads`` is the sequence of read_namespaced_pod outcomes: a pod, or an
    ApiException to raise (404 once the pod has really gone).
    """

    def __init__(self, reads, delete_error=None, forever=False):
        self._reads = list(reads)
        self._delete_error = delete_error
        self._forever = forever  # Never 404: the pod that will not go
        self.deleted: list[str] = []

    def read_namespaced_pod(self, name, namespace):
        if self._forever:
            return self._reads[0]
        outcome = self._reads.pop(0) if self._reads else ApiException(status=404)
        if isinstance(outcome, ApiException):
            raise outcome
        return outcome

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds=None):
        if self._delete_error:
            raise self._delete_error
        self.deleted.append(name)


def test_delete_reports_success():
    api = FakeDeleteApi([_pod("gone", "Failed", 0), ApiException(status=404)])
    assert _backend(api).delete(DeleteOptions("gone")) is True
    assert api.deleted == ["gone"]


def test_delete_reports_a_refused_delete(capsys):
    # A role that can create pods but not delete them made every --rm a no-op,
    # and said nothing about it. It has to reach stderr.
    api = FakeDeleteApi([_pod("stuck", "Failed", 0)], delete_error=ApiException(403))
    assert _backend(api).delete(DeleteOptions("stuck")) is False
    assert "Could not delete pod stuck" in capsys.readouterr().err


def test_delete_gives_up_on_a_pod_that_will_not_go(capsys):
    # A pod held by a finalizer never 404s; this used to spin forever.
    backend = _backend(FakeDeleteApi([_pod("zombie", "Failed", 0)], forever=True))
    backend._polling_freq = 1000  # Don't really wait a minute
    backend._delete_timeout = 0.05
    assert backend.delete(DeleteOptions("zombie")) is False
    assert "still present" in capsys.readouterr().err


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


def _volume_manifest(spec: str):
    _, manifest, volumes = build_pod_manifest(
        RunOptions(image="busybox", volumes=[spec]), _log
    )
    return manifest["spec"], volumes


def test_volume_dir_mounts_the_directory(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    spec, volumes = _volume_manifest(f"{src}:/data")
    assert spec["containers"][0]["volumeMounts"] == [
        {"name": "shared-data-0", "mountPath": "/data"}
    ]
    assert spec["initContainers"][0]["volumeMounts"] == [
        {"name": "shared-data-0", "mountPath": "/data"}
    ]
    assert volumes == [{"src": src, "dst": Path("/data")}]


def test_volume_file_mounts_only_the_file(tmp_path):
    # The parent directory must not be mounted over: everything else the
    # image has in it has to survive, and a readOnly mount must cover the
    # file alone.
    src = tmp_path / "cfg.yml"
    src.write_text("x")
    spec, volumes = _volume_manifest(f"{src}:/etc/app/cfg.yml")
    assert spec["containers"][0]["volumeMounts"] == [
        {
            "name": "shared-data-0",
            "mountPath": "/etc/app/cfg.yml",
            "subPath": "cfg.yml",
        }
    ]
    # The init container fills the staged copy, out of the way of the image.
    assert spec["initContainers"][0]["volumeMounts"] == [
        {"name": "shared-data-0", "mountPath": "/kodman-volumes/shared-data-0"}
    ]
    assert volumes == [
        {"src": src, "dst": Path("/kodman-volumes/shared-data-0/cfg.yml")}
    ]


def test_volume_file_renamed_by_the_destination(tmp_path):
    src = tmp_path / "from.txt"
    src.write_text("x")
    spec, volumes = _volume_manifest(f"{src}:/test/to.txt")
    assert spec["containers"][0]["volumeMounts"][0]["subPath"] == "to.txt"
    assert volumes[0]["dst"] == Path("/kodman-volumes/shared-data-0/to.txt")


def test_volume_file_at_the_root_is_supported(tmp_path):
    # Used to raise NotImplementedError, because mounting the emptyDir at the
    # file's parent meant mounting it over /.
    src = tmp_path / "to_read.txt"
    src.write_text("x")
    spec, _ = _volume_manifest(f"{src}:/to_read.txt")
    assert spec["containers"][0]["volumeMounts"] == [
        {
            "name": "shared-data-0",
            "mountPath": "/to_read.txt",
            "subPath": "to_read.txt",
        }
    ]


def test_volume_destination_root_is_an_error(tmp_path):
    # Used to be a NotImplementedError from the parent-directory mount; now
    # there is no parent to stage under, so it is rejected up front.
    src = tmp_path / "x.txt"
    src.write_text("x")
    with pytest.raises(ValueError, match="must not be '/'"):
        _volume_manifest(f"{src}:/")


def test_two_files_into_one_directory_do_not_collide(tmp_path):
    first = tmp_path / "one.conf"
    first.write_text("1")
    second = tmp_path / "two.conf"
    second.write_text("2")
    options = RunOptions(
        image="busybox",
        volumes=[f"{first}:/etc/one.conf", f"{second}:/etc/two.conf"],
    )
    _, manifest, _ = build_pod_manifest(options, _log)
    # Two mounts at the same mountPath would have shadowed each other.
    mount_paths = [
        m["mountPath"] for m in manifest["spec"]["containers"][0]["volumeMounts"]
    ]
    assert mount_paths == ["/etc/one.conf", "/etc/two.conf"]


def test_volume_ro_makes_only_the_workload_mount_read_only(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    spec, _ = _volume_manifest(f"{src}:/data:ro")
    assert spec["containers"][0]["volumeMounts"][0]["readOnly"] is True
    # The init container still has to copy the data in.
    assert "readOnly" not in spec["initContainers"][0]["volumeMounts"][0]


@pytest.mark.parametrize(
    "options", ["z", "Z", "rw", "rw,z", "ro,Z", "rslave", "cached", "ro,delegated"]
)
def test_volume_docker_options_are_accepted(tmp_path, options):
    src = tmp_path / "data"
    src.mkdir()
    spec, _ = _volume_manifest(f"{src}:/data:{options}")
    mount = spec["containers"][0]["volumeMounts"][0]
    assert mount.get("readOnly", False) == ("ro" in options.split(","))


def test_volume_unknown_option_is_an_error(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    with pytest.raises(ValueError, match="Unsupported volume option: nocopy"):
        _volume_manifest(f"{src}:/data:nocopy")


def test_volume_too_many_fields_is_an_error(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    with pytest.raises(ValueError, match="Invalid volume specification"):
        _volume_manifest(f"{src}:/data:ro:extra")


def test_volume_ro_on_a_file_covers_only_the_file(tmp_path):
    # With subPath staging (#62) 'ro' on a file mount covers just that file,
    # same as docker - unlike the old parent-directory mount, which used to
    # widen it to the whole containing directory and warn about doing so.
    src = tmp_path / "cfg.yml"
    src.write_text("x")
    spec, _ = _volume_manifest(f"{src}:/etc/app/cfg.yml:ro")
    assert spec["containers"][0]["volumeMounts"] == [
        {
            "name": "shared-data-0",
            "mountPath": "/etc/app/cfg.yml",
            "subPath": "cfg.yml",
            "readOnly": True,
        }
    ]
