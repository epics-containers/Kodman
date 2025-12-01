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
    pod_command = "pip install /kodman > /dev/null 2>&1 && kodman run --rm hello-world"
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

    subprocess.run(cmd, capture_output=True, text=True)
    assert responses.hello_world in subprocess.check_output(cmd).decode().strip()


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_docker_run_exitcodes():
    ERROR_MSG = "My error message"
    cmd = [
        DOCKER_PROVIDER,
        "run",
        "--entrypoint",
        "bash",
        "--rm",
        "ubuntu",
        "-c",
        f"echo '{ERROR_MSG}' >&2; exit 1",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.strip() == ERROR_MSG


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_exitcodes():
    ERROR_MSG = "My error message"
    cmd = [
        ENTRY_POINT,
        "run",
        "--entrypoint",
        "bash",
        "--rm",
        "ubuntu",
        "-c",
        f"echo '{ERROR_MSG}' >&2; exit 1",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 1
    assert (
        result.stdout.strip() == ERROR_MSG
    )  # K8s does not distinguish between stderr and stdout!


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_docker_run_mount_dir(data: Path):
    FILE_MOUNT = "to_mount.txt"
    cmd = [
        DOCKER_PROVIDER,
        "run",
        "-v",
        f"{data}:/test",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{FILE_MOUNT}",
    ]
    assert subprocess.check_output(cmd).decode().strip() == responses.mount


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_mount_dir(data: Path):
    FILE_MOUNT = "to_mount.txt"
    cmd = [
        ENTRY_POINT,
        "run",
        "-v",
        f"{data}:/test",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{FILE_MOUNT}",
    ]

    assert subprocess.check_output(cmd).decode().strip() == responses.mount


def test_docker_run_mount_file(data: Path):
    FILE_MOUNT = "to_mount.txt"
    FILE_NEW = "to_read.txt"
    cmd = [
        DOCKER_PROVIDER,
        "run",
        "-v",
        f"{data}/{FILE_MOUNT}:/test/{FILE_NEW}",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{FILE_NEW}",
    ]

    assert subprocess.check_output(cmd).decode().strip() == responses.mount


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_mount_file(data: Path):
    FILE_MOUNT = "to_mount.txt"
    FILE_NEW = "to_read.txt"
    cmd = [
        ENTRY_POINT,
        "run",
        "-v",
        f"{data}/{FILE_MOUNT}:/test/{FILE_NEW}",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{FILE_NEW}",
    ]

    assert subprocess.check_output(cmd).decode().strip() == responses.mount


@pytest.mark.skipif(
    not KODMAN_SYSTEM_TESTING, reason="export KODMAN_SYSTEM_TESTING=true"
)
def test_kodman_run_mount_root(data: Path):
    FILE_MOUNT = "to_mount.txt"
    FILE_NEW = "to_read.txt"
    cmd = [
        ENTRY_POINT,
        "run",
        "-v",
        f"{data}/{FILE_MOUNT}:/{FILE_NEW}",
        "--rm",
        "ubuntu",
        "bash",
        "-c",
        f"cat test/{FILE_NEW}",
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
