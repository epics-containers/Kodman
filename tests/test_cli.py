import subprocess

from data import responses
from kodman import __version__

ENTRY_POINT = "kodman"


def test_cli_version():
    cmd = [ENTRY_POINT, "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__


def test_cli_help():
    cmd = [ENTRY_POINT, "--help"]
    assert responses.help_screen in subprocess.check_output(cmd).decode().strip()
