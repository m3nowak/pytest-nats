import hashlib
import io
import os
import platform
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import respx
from pytest import MonkeyPatch, fixture, mark, raises

from pytest_nats._provisioning import (
    AcquisitionError,
    ErrorCategory,
    ProvisioningConfig,
    build_nats_command,
)

_RELEASES_API = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
_DOWNLOAD_ROOT = "https://github.com/nats-io/nats-server/releases/download"


def _versions() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class ReleaseArtifact:
    version: str
    archive_name: str
    archive: bytes
    cache_system: str
    cache_architecture: str
    executable_name: str

    def executable(self, cache_root: Path) -> Path:
        return cache_root / self.version / self.cache_system / self.cache_architecture / self.executable_name


@dataclass
class ProcessHarness:
    CalledProcessError = subprocess.CalledProcessError

    installation: Path
    version: str = "2.14.6"
    versions: dict[str, str] = field(default_factory=_versions)
    mise_failure: Exception | None = None

    @property
    def executable(self) -> Path:
        return self.installation / _platform_names()[4]

    def run(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            version = self.versions.get(command[0], self.version)
            return subprocess.CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")
        if command[1] == "install":
            if self.mise_failure is not None:
                raise self.mise_failure
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        self.installation.mkdir(parents=True, exist_ok=True)
        self.executable.touch()
        return subprocess.CompletedProcess(command, 0, stdout=f"{self.installation}\n", stderr="")


@fixture
def processes(tmp_path: Path, monkeypatch: MonkeyPatch) -> ProcessHarness:
    harness = ProcessHarness(tmp_path / "mise-installation")
    monkeypatch.setattr("pytest_nats._provisioning.subprocess", harness)
    return harness


def _platform_names() -> tuple[str, str, str, str, str]:
    cache_system, archive_system = {
        "Linux": ("linux", "linux"),
        "Darwin": ("macos", "darwin"),
        "Windows": ("windows", "windows"),
    }[platform.system()]
    cache_architecture, archive_architecture = {
        "x86_64": ("amd64", "amd64"),
        "AMD64": ("amd64", "amd64"),
        "aarch64": ("arm64", "arm64"),
        "arm64": ("arm64", "arm64"),
        "ARM64": ("arm64", "arm64"),
    }[platform.machine()]
    executable_name = "nats-server.exe" if cache_system == "windows" else "nats-server"
    return cache_system, cache_architecture, archive_system, archive_architecture, executable_name


def _artifact(version: str, content: bytes = b"nats server") -> ReleaseArtifact:
    cache_system, cache_architecture, archive_system, archive_architecture, executable_name = _platform_names()
    extension = "zip" if cache_system == "windows" else "tar.gz"
    archive_name = f"nats-server-v{version}-{archive_system}-{archive_architecture}.{extension}"
    member_name = f"nats-server-v{version}-{archive_system}-{archive_architecture}/{executable_name}"
    archive_buffer = io.BytesIO()
    if extension == "zip":
        with zipfile.ZipFile(archive_buffer, mode="w") as archive:
            archive.writestr(member_name, content)
    else:
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo(member_name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return ReleaseArtifact(
        version,
        archive_name,
        archive_buffer.getvalue(),
        cache_system,
        cache_architecture,
        executable_name,
    )


def _mock_download(httpx2_mock: respx.Router, artifact: ReleaseArtifact) -> tuple[respx.Route, respx.Route]:
    release_url = f"{_DOWNLOAD_ROOT}/v{artifact.version}"
    archive_route = httpx2_mock.get(f"{release_url}/{artifact.archive_name}").respond(content=artifact.archive)
    checksum = hashlib.sha256(artifact.archive).hexdigest()
    checksum_route = httpx2_mock.get(f"{release_url}/SHA256SUMS").respond(
        content=f"{checksum}  {artifact.archive_name}\n".encode()
    )
    return archive_route, checksum_route


def _put_mise_on_path(directory: Path, monkeypatch: MonkeyPatch) -> None:
    directory.mkdir()
    mise = directory / ("mise.exe" if os.name == "nt" else "mise")
    mise.touch()
    mise.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory))


def test_user_supplied_executable_is_validated_and_returned(
    tmp_path: Path,
    processes: ProcessHarness,
) -> None:
    executable = tmp_path / "nats-server"
    executable.touch()

    command = build_nats_command(ProvisioningConfig(executable=executable))

    assert command == (str(executable.resolve()),)


def test_user_supplied_executable_must_report_nats_2(
    tmp_path: Path,
    processes: ProcessHarness,
) -> None:
    executable = tmp_path / "nats-server"
    executable.touch()
    processes.version = "1.4.0"

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(executable=executable))

    assert raised.value.category is ErrorCategory.PROVISIONING


@mark.parametrize("version", ["2.0.0", "2.1.9", "2.2.0-rc1", "3.0.0"])
def test_user_supplied_executable_must_report_a_supported_release(
    version: str,
    tmp_path: Path,
    processes: ProcessHarness,
) -> None:
    executable = tmp_path / "nats-server"
    executable.touch()
    processes.version = version

    with raises(AcquisitionError, match="supported NATS release") as raised:
        build_nats_command(ProvisioningConfig(executable=executable))

    assert raised.value.category is ErrorCategory.PROVISIONING


def test_conflicting_configuration_is_rejected(tmp_path: Path) -> None:
    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.14.6", executable=tmp_path / "nats-server"))

    assert raised.value.category is ErrorCategory.INVALID_CONFIGURATION


@mark.parametrize("selector", ["Latest", "v2.14.6", "2.14.6-rc1", "3", 2])
def test_invalid_version_selector_is_rejected(selector: object) -> None:
    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=cast(Any, selector), provider="github"))

    assert raised.value.category is ErrorCategory.INVALID_CONFIGURATION


@mark.parametrize("selector", ["2.0", "2.1.9"])
def test_managed_selector_must_include_a_supported_release(selector: str) -> None:
    with raises(AcquisitionError, match="supported NATS release") as raised:
        build_nats_command(ProvisioningConfig(version=selector, provider="github"))

    assert raised.value.category is ErrorCategory.INVALID_CONFIGURATION


def test_github_provisions_an_exact_release_and_reuses_the_cache(
    tmp_path: Path,
    processes: ProcessHarness,
    httpx2_mock: respx.Router,
    monkeypatch: MonkeyPatch,
) -> None:
    artifact = _artifact("2.14.6")
    archive_route, checksum_route = _mock_download(httpx2_mock, artifact)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    config = ProvisioningConfig(version=artifact.version, cache_dir=tmp_path)

    first = build_nats_command(config)
    second = build_nats_command(config)

    executable = artifact.executable(tmp_path)
    assert first == second == (str(executable),)
    assert executable.read_bytes() == b"nats server"
    assert archive_route.call_count == checksum_route.call_count == 1


@mark.parametrize(
    ("selector", "resolved_version"),
    [(None, "2.15.0"), ("2", "2.15.0"), ("2.14", "2.14.10")],
)
def test_github_resolves_stable_version_selectors(
    selector: str | None,
    resolved_version: str,
    tmp_path: Path,
    processes: ProcessHarness,
    httpx2_mock: respx.Router,
    monkeypatch: MonkeyPatch,
) -> None:
    artifact = _artifact(resolved_version)
    processes.version = artifact.version
    api_route = httpx2_mock.get(_RELEASES_API).respond(
        json=[
            {"tag_name": "v2.14.9", "draft": False, "prerelease": False},
            {"tag_name": "v2.14.10", "draft": False, "prerelease": False},
            {"tag_name": "v2.15.0", "draft": False, "prerelease": False},
            {"tag_name": "v2.16.0", "draft": True, "prerelease": False},
            {"tag_name": "v2.15.1", "draft": False, "prerelease": True},
        ]
    )
    _mock_download(httpx2_mock, artifact)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    command = build_nats_command(ProvisioningConfig(version=selector, provider="github", cache_dir=tmp_path))

    assert command == (str(artifact.executable(tmp_path)),)
    call = api_route.calls.last
    assert call is not None
    assert call.request.headers["authorization"] == "Bearer test-token"


def test_github_repairs_an_invalid_cache_entry(
    tmp_path: Path,
    processes: ProcessHarness,
    httpx2_mock: respx.Router,
) -> None:
    artifact = _artifact("2.14.6", b"repaired server")
    executable = artifact.executable(tmp_path)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"damaged server")
    processes.versions[str(executable)] = "2.13.0"
    _mock_download(httpx2_mock, artifact)

    command = build_nats_command(ProvisioningConfig(version=artifact.version, provider="github", cache_dir=tmp_path))

    assert command == (str(executable),)
    assert executable.read_bytes() == b"repaired server"


def test_github_does_not_publish_an_archive_with_a_bad_checksum(
    tmp_path: Path,
    httpx2_mock: respx.Router,
) -> None:
    artifact = _artifact("2.14.6")
    release_url = f"{_DOWNLOAD_ROOT}/v{artifact.version}"
    httpx2_mock.get(f"{release_url}/{artifact.archive_name}").respond(content=artifact.archive)
    httpx2_mock.get(f"{release_url}/SHA256SUMS").respond(content=f"{'0' * 64}  {artifact.archive_name}\n".encode())

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=artifact.version, provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert not artifact.executable(tmp_path).exists()


def test_release_resolution_failure_is_categorized(
    tmp_path: Path,
    httpx2_mock: respx.Router,
) -> None:
    httpx2_mock.get(_RELEASES_API).respond(json=[])

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.12", provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.VERSION_RESOLUTION


def test_github_download_failure_is_categorized(
    tmp_path: Path,
    httpx2_mock: respx.Router,
) -> None:
    artifact = _artifact("2.14.6")
    release_url = f"{_DOWNLOAD_ROOT}/v{artifact.version}"
    httpx2_mock.get(f"{release_url}/{artifact.archive_name}").respond(404)

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=artifact.version, provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.PROVISIONING


def test_auto_uses_available_mise(
    tmp_path: Path,
    processes: ProcessHarness,
    httpx2_mock: respx.Router,
    monkeypatch: MonkeyPatch,
) -> None:
    _put_mise_on_path(tmp_path / "bin", monkeypatch)

    command = build_nats_command(ProvisioningConfig(version="2.14.6"))

    assert command == (str(processes.executable.resolve()),)
    assert not httpx2_mock.calls.called


def test_auto_does_not_fall_back_when_mise_fails(
    tmp_path: Path,
    processes: ProcessHarness,
    httpx2_mock: respx.Router,
    monkeypatch: MonkeyPatch,
) -> None:
    _put_mise_on_path(tmp_path / "bin", monkeypatch)
    failure = subprocess.CalledProcessError(1, ["mise", "install"], stderr="mise failed")
    processes.mise_failure = failure

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.14.6"))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert raised.value.__cause__ is failure
    assert not httpx2_mock.calls.called


def test_forced_mise_reports_when_mise_is_unavailable(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.14.6", provider="mise"))

    assert raised.value.category is ErrorCategory.PROVISIONING
