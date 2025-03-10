import os
from pathlib import Path
from typing import Any

import pytest

# Prevent pytest from catching exceptions when debugging in vscode so that break on
# exception works correctly (see: https://github.com/pytest-dev/pytest/issues/7409)
if os.getenv("PYTEST_RAISE", "0") == "1":

    @pytest.hookimpl(tryfirst=True)
    def pytest_exception_interact(call: pytest.CallInfo[Any]):
        if call.excinfo is not None:
            raise call.excinfo.value
        else:
            raise RuntimeError(
                f"{call} has no exception data, an unknown error has occurred"
            )

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(excinfo: pytest.ExceptionInfo[Any]):
        raise excinfo.value


DATA_PATH = Path(__file__).parent / "data"


@pytest.fixture
def data() -> Path:
    return DATA_PATH


ROOT_PATH = Path(__file__).parent.parent


@pytest.fixture
def root() -> Path:
    return ROOT_PATH


@pytest.fixture
def env_vars(mocker):
    mocker.patch.dict(
        os.environ,
        {
            "KODMAN_TEST_STRING": "Test",
            "KODMAN_TEST_BOOL_TRUE": "true",
            "KODMAN_TEST_BOOL_FALSE": "false",
            "KODMAN_TEST_INT": "99",
        },
    )
