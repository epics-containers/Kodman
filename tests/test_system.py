import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from data import responses

ENTRY_POINT = "kodman"
DOCKER_PROVIDER = os.getenv("DOCKER_PROVIDER", "podman")
KODMAN_SYSTEM_TESTING = os.getenv("KODMAN_SYSTEM_TESTING") == "true"


def remove_empty_lines(text):
    """Remove after solving https://github.com/epics-containers/Kodman/issues/9"""
    lines = [line for line in text.split("\n") if line.strip()]
    return "\n".join(lines)


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_docker_run_hello():
    cmd = [DOCKER_PROVIDER, "run", "--rm", "hello-world"]
    assert responses.hello_world in subprocess.check_output(cmd).decode().strip()


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_hello():
    cmd = [ENTRY_POINT, "run", "--rm", "hello-world"]
    assert responses.hello_world in subprocess.check_output(cmd).decode().strip()


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_incluster(root: Path):
    # The repo is bind-mounted into the pod with the host's uid while the
    # container runs as root, so git refuses to introspect it ("detected
    # dubious ownership") and setuptools_scm can't derive a version, failing
    # the build. Mark the mount safe before installing.
    #
    # Don't suppress pip output: when this fails it is almost always the build
    # inside the pod, and the kodman-streamed pod logs are the only diagnostic
    # we get back from CI.
    pod_command = (
        "git config --global --add safe.directory /kodman"
        " && pip install /kodman"
        " && kodman run --rm hello-world"
    )
    cmd = [
        ENTRY_POINT,
        "run",
        "--rm",
        "--entrypoint",
        "bash",
        "-v",
        f"{root}:/kodman",
        f"python:{sys.version_info.major}.{sys.version_info.minor}",
        "-c",
        pod_command,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    # Surface the inner pod logs on failure; check_output would hide them inside
    # CalledProcessError.output, which pytest does not print.
    assert responses.hello_world in result.stdout, (
        f"exit={result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_docker_run_exitcodes():
    error_msg = "My error message"
    cmd = [
        DOCKER_PROVIDER,
        "run",
        "--entrypoint",
        "bash",
        "--rm",
        "ubuntu",
        "-c",
        f"echo '{error_msg}' >&2; exit 1",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.strip() == error_msg


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_exitcodes():
    error_msg = "My error message"
    cmd = [
        ENTRY_POINT,
        "run",
        "--entrypoint",
        "bash",
        "--rm",
        "ubuntu",
        "-c",
        f"echo '{error_msg}' >&2; exit 1",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 1
    assert (
        result.stdout.strip() == error_msg
    )  # K8s does not distinguish between stderr and stdout!


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_docker_run_mount_dir(data: Path):
    file_mount = "to_mount.txt"
    cmd = [
        DOCKER_PROVIDER,
        "run",
        "-v",
        f"{data}:/test",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{file_mount}",
    ]
    assert subprocess.check_output(cmd).decode().strip() == responses.mount


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_mount_dir(data: Path):
    file_mount = "to_mount.txt"
    cmd = [
        ENTRY_POINT,
        "run",
        "-v",
        f"{data}:/test",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{file_mount}",
    ]

    assert subprocess.check_output(cmd).decode().strip() == responses.mount


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_docker_run_mount_file(data: Path):
    file_mount = "to_mount.txt"
    file_new = "to_read.txt"
    cmd = [
        DOCKER_PROVIDER,
        "run",
        "-v",
        f"{data}/{file_mount}:/test/{file_new}",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{file_new}",
    ]

    assert subprocess.check_output(cmd).decode().strip() == responses.mount


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_mount_file(data: Path):
    file_mount = "to_mount.txt"
    file_new = "to_read.txt"
    cmd = [
        ENTRY_POINT,
        "run",
        "-v",
        f"{data}/{file_mount}:/test/{file_new}",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{file_new}",
    ]

    assert subprocess.check_output(cmd).decode().strip() == responses.mount


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_mount_root(data: Path):
    file_mount = "to_mount.txt"
    file_new = "to_read.txt"
    cmd = [
        ENTRY_POINT,
        "run",
        "-v",
        f"{data}/{file_mount}:/{file_new}",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{file_new}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 1


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_mount_large(data: Path):
    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_dir = Path(tmpdirname)
        file_slug = "test.txt"
        file_name = temp_dir / file_slug
        with open(file_name, "wb") as out:
            out.truncate(100 * 1024 * 1024)

        cmd = [
            ENTRY_POINT,
            "run",
            "-v",
            f"{tmpdirname}:/test",
            "--rm",
            "ubuntu",
            "bash",
            "-c",
            f"[ -f test/{file_slug} ] && echo pass",
        ]

        assert subprocess.check_output(cmd).decode().strip() == "pass"


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_fail_image(data: Path):
    cmd = [
        ENTRY_POINT,
        "run",
        "--rm",
        "hello-worl",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 1
    assert responses.failed_image in result.stderr


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_fail_command(data: Path):
    cmd = [
        ENTRY_POINT,
        "run",
        "--rm",
        "--entrypoint",
        "bash",
        "hello-world",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 1
    assert responses.failed_command in result.stderr


def _kodman_pod_names() -> set[str]:
    out = subprocess.check_output(["kubectl", "get", "pods", "-o", "json"]).decode()
    pods = json.loads(out)["items"]
    return {
        p["metadata"]["name"]
        for p in pods
        if p["metadata"]["name"].startswith("kodman-run-")
    }


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_failed_pod_does_not_restart():
    """Regression for the boot-loop bug.

    A failed run-to-completion pod must settle in phase ``Failed`` with zero
    restarts. Before the fix the pod inherited the Pod default
    ``restartPolicy=Always``, so a failed container was restarted indefinitely
    (CrashLoopBackOff) and spammed alerts. We omit ``--rm`` so the pod survives
    for inspection, then delete it ourselves.
    """
    before = _kodman_pod_names()
    cmd = [
        ENTRY_POINT,
        "run",
        "--entrypoint",
        "bash",
        "ubuntu",
        "-c",
        "echo boom; exit 7",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Exit code is propagated even though the pod is left behind.
    assert result.returncode == 7, (
        f"exit={result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    new_pods = _kodman_pod_names() - before
    assert len(new_pods) == 1, f"expected one new pod, got {new_pods}"
    pod_name = new_pods.pop()

    try:
        pod = json.loads(
            subprocess.check_output(
                ["kubectl", "get", "pod", pod_name, "-o", "json"]
            ).decode()
        )
        assert pod["spec"]["restartPolicy"] == "Never"
        assert pod["status"]["phase"] == "Failed", pod["status"]
        exec_status = next(
            c for c in pod["status"]["containerStatuses"] if c["name"] == "kodman-exec"
        )
        assert exec_status["restartCount"] == 0, exec_status
    finally:
        subprocess.run(
            ["kubectl", "delete", "pod", pod_name, "--ignore-not-found"],
            check=False,
        )
