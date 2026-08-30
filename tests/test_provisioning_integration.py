import os
import shutil
from pathlib import Path

import pytest

from pytest_nats import GitHub, Mise
from pytest_nats._provisioning import acquire_nats

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PYTEST_NATS_RUN_INTEGRATION") != "1",
        reason="set PYTEST_NATS_RUN_INTEGRATION=1 to run real provider tests",
    ),
]

_INTEGRATION_VERSION = "2.12.15"


def test_github_provisions_a_fixed_release(tmp_path: Path) -> None:
    command = acquire_nats(GitHub(_INTEGRATION_VERSION, cache_dir=tmp_path), tmp_path).command

    assert Path(command[0]).is_file()


def test_mise_provisions_a_fixed_release() -> None:
    if shutil.which("mise") is None:
        pytest.skip("mise is not available on PATH")

    command = acquire_nats(Mise(_INTEGRATION_VERSION), Path.cwd()).command

    assert Path(command[0]).is_file()
