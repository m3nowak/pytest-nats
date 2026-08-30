import os
import shutil
from pathlib import Path

import pytest

from pytest_nats._provisioning import ProvisioningConfig, build_nats_command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PYTEST_NATS_RUN_INTEGRATION") != "1",
        reason="set PYTEST_NATS_RUN_INTEGRATION=1 to run real provider tests",
    ),
]

_INTEGRATION_VERSION = "2.12.15"


def test_github_provisions_a_fixed_release(tmp_path: Path) -> None:
    command = build_nats_command(
        ProvisioningConfig(
            version=_INTEGRATION_VERSION,
            provider="github",
            cache_dir=tmp_path,
        )
    )

    assert Path(command[0]).is_file()


def test_mise_provisions_a_fixed_release() -> None:
    if shutil.which("mise") is None:
        pytest.skip("mise is not available on PATH")

    command = build_nats_command(ProvisioningConfig(version=_INTEGRATION_VERSION, provider="mise"))

    assert Path(command[0]).is_file()
