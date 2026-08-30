import hashlib
import io
import logging
import os
import tarfile
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, TimeoutExpired
from threading import Barrier
from types import TracebackType
from typing import Any, Self, cast

from pytest import CaptureFixture, LogCaptureFixture, MonkeyPatch, mark, raises, skip

from pytest_nats._provisioning import (
    AcquisitionError,
    ErrorCategory,
    ProvisioningConfig,
    build_nats_command,
)


class FakeResponse:
    def __init__(
        self,
        content: bytes = b"",
        payload: object = None,
        status_error: Exception | None = None,
    ) -> None:
        self.content = content
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    def json(self) -> object:
        return self._payload

    def iter_bytes(self) -> Iterator[bytes]:
        yield self.content

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass


class FakeClient:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    def get(self, url: str) -> FakeResponse:
        self.requests.append(url)
        return self.responses[url]

    def stream(self, method: str, url: str) -> FakeResponse:
        assert method == "GET"
        self.requests.append(url)
        return self.responses[url]


def make_tar_archive(member_name: str, content: bytes) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        info = tarfile.TarInfo(member_name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return archive.getvalue()


def make_zip_archive(member_name: str, content: bytes) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w") as zipped:
        zipped.writestr(member_name, content)
    return archive.getvalue()


def test_user_supplied_nats_executable_returns_an_absolute_command(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    executable = tmp_path / "nats-server"
    executable.touch()

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        assert command == [str(executable), "--version"]
        return CompletedProcess(command, 0, stdout="nats-server: v2.14.6\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)

    command = build_nats_command(ProvisioningConfig(executable=executable))

    assert command == (str(executable.resolve()),)


def test_user_supplied_executable_rejects_a_version_selector(tmp_path: Path) -> None:
    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(executable=tmp_path / "nats-server", version="2.14.6"))

    assert raised.value.category is ErrorCategory.INVALID_CONFIGURATION


def test_user_supplied_symlink_accepts_a_nats_prerelease_outside_managed_platforms(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    executable = tmp_path / "installation" / "nats-server"
    executable.parent.mkdir()
    executable.touch()
    link = tmp_path / "nats-server"
    try:
        link.symlink_to(executable)
    except OSError as exc:
        skip(f"the test environment cannot create symlinks: {exc}")

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        assert command == [str(executable), "--version"]
        return CompletedProcess(command, 0, stdout="nats-server: v2.15.0-RC.1\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "FreeBSD")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "riscv64")

    assert build_nats_command(ProvisioningConfig(executable=link)) == (str(executable),)


def test_user_supplied_validation_timeout_is_a_provisioning_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    timeout = TimeoutExpired("nats-server", 5)

    def run(_command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        raise timeout

    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(executable=tmp_path / "nats-server"))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert raised.value.__cause__ is timeout


def test_user_supplied_version_process_failure_preserves_its_cause(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    failure = CalledProcessError(1, ["nats-server", "--version"], stderr="broken executable")

    def run(_command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        raise failure

    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(executable=tmp_path / "nats-server"))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert raised.value.__cause__ is failure


@mark.parametrize(
    "selector",
    ["", "Latest", "v2.14.6", " 2", "2 ", "2.14.6-rc1", "2.14.6.1", 2, "3"],
)
def test_malformed_or_unsupported_version_selector_is_rejected(selector: object) -> None:
    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=cast(Any, selector), provider="github"))

    assert raised.value.category is ErrorCategory.INVALID_CONFIGURATION


def test_exact_github_release_is_verified_and_published_to_the_cache(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"server binary")
    digest = hashlib.sha256(archive).hexdigest()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**kwargs: Any) -> FakeClient:
        assert kwargs == {"timeout": 30, "follow_redirects": True, "headers": {}}
        return client

    def run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        assert command[1:] == ["--version"]
        assert kwargs["timeout"] == 5
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    command = build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    executable = tmp_path / version / "linux" / "amd64" / "nats-server"
    assert command == (str(executable),)
    assert executable.read_bytes() == b"server binary"
    if os.name != "nt":
        assert executable.stat().st_mode & 0o111
    assert client.requests == [f"{release_url}/{archive_name}", f"{release_url}/SHA256SUMS"]


def test_valid_github_cache_entry_is_revalidated_without_network_access(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    executable = tmp_path / "2.14.6" / "linux" / "amd64" / "nats-server"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"cached server")

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        assert command == [str(executable), "--version"]
        return CompletedProcess(command, 0, stdout="nats-server: v2.14.6\n", stderr="")

    def no_client(**_kwargs: object) -> None:
        raise AssertionError("a cache hit must not use the network")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", no_client)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    command = build_nats_command(ProvisioningConfig(version="2.14.6", provider="github", cache_dir=tmp_path))

    assert command == (str(executable),)


def test_partial_selector_resolves_the_greatest_matching_stable_release(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.10"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"server binary")
    digest = hashlib.sha256(archive).hexdigest()
    api_url = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            api_url: FakeResponse(
                payload=[
                    {"tag_name": "v2.14.9", "draft": False, "prerelease": False},
                    {"tag_name": "v2.15.0", "draft": False, "prerelease": False},
                    {"tag_name": "v2.14.10", "draft": False, "prerelease": False},
                    {"tag_name": "v2.14.11", "draft": True, "prerelease": False},
                    {"tag_name": "v2.14.12", "draft": False, "prerelease": True},
                    {"tag_name": "not-a-version", "draft": False, "prerelease": False},
                ]
            ),
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    command = build_nats_command(ProvisioningConfig(version="2.14", provider="github", cache_dir=tmp_path))

    assert command == (str(tmp_path / version / "linux" / "amd64" / "nats-server"),)
    assert client.requests[0] == api_url


def test_auto_uses_available_mise_without_editing_mise_configuration(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    mise = tmp_path / "bin" / "mise"
    installation = tmp_path / "mise-installation"
    executable = installation / "nats-server"
    calls: list[tuple[list[str], object]] = []

    def run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((command, kwargs["timeout"]))
        if command[1] == "install":
            return CompletedProcess(command, 0, stdout="", stderr="")
        if command[1] == "where":
            installation.mkdir()
            executable.touch()
            return CompletedProcess(command, 0, stdout=f"{installation}\n", stderr="")
        return CompletedProcess(command, 0, stdout="nats-server: v2.14.6\n", stderr="")

    def which(_name: str) -> str:
        return str(mise)

    monkeypatch.setattr("pytest_nats._provisioning.shutil.which", which)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    command = build_nats_command(ProvisioningConfig(version="2.14.6"))

    backend = "github:nats-io/nats-server@2.14.6"
    assert command == (str(executable),)
    assert calls == [
        ([str(mise), "install", backend], 300),
        ([str(mise), "where", backend], 300),
        ([str(executable), "--version"], 5),
    ]


def test_auto_does_not_fall_back_when_available_mise_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    failure = CalledProcessError(1, ["mise", "install"], stderr="mise is broken")

    def which(_name: str) -> str:
        return str(tmp_path / "mise")

    def run(_command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        raise failure

    def no_client(**_kwargs: object) -> None:
        raise AssertionError("mise failure must not fall back to GitHub")

    monkeypatch.setattr("pytest_nats._provisioning.shutil.which", which)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", no_client)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.14.6"))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert raised.value.__cause__ is failure
    assert "mise is broken" in str(raised.value)


def test_forced_mise_reports_when_mise_is_unavailable(monkeypatch: MonkeyPatch) -> None:
    def which(_name: str) -> None:
        return None

    monkeypatch.setattr("pytest_nats._provisioning.shutil.which", which)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.14.6", provider="mise"))

    assert raised.value.category is ErrorCategory.PROVISIONING


def test_checksum_mismatch_does_not_publish_or_leave_temporary_files(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"server binary")
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{'0' * 64}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.PROVISIONING
    target_directory = tmp_path / version / "linux" / "amd64"
    assert list(target_directory.iterdir()) == []


def test_windows_arm64_uses_the_official_zip_and_executable_names(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-windows-arm64.zip"
    member_name = f"nats-server-v{version}-windows-arm64/nats-server.exe"
    archive = make_zip_archive(member_name, b"windows server")
    digest = hashlib.sha256(archive).hexdigest()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Windows")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "ARM64")

    command = build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    executable = tmp_path / version / "windows" / "arm64" / "nats-server.exe"
    assert command == (str(executable),)
    assert executable.read_bytes() == b"windows server"
    assert client.requests[0].endswith(archive_name)


@mark.parametrize(
    ("system", "machine", "cache_system", "cache_architecture", "executable_name"),
    [
        ("Linux", "x86_64", "linux", "amd64", "nats-server"),
        ("Linux", "aarch64", "linux", "arm64", "nats-server"),
        ("Darwin", "x86_64", "macos", "amd64", "nats-server"),
        ("Darwin", "arm64", "macos", "arm64", "nats-server"),
        ("Windows", "AMD64", "windows", "amd64", "nats-server.exe"),
        ("Windows", "ARM64", "windows", "arm64", "nats-server.exe"),
    ],
)
def test_managed_platform_matrix_accepts_cached_executables(
    system: str,
    machine: str,
    cache_system: str,
    cache_architecture: str,
    executable_name: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    executable = tmp_path / "2.14.6" / cache_system / cache_architecture / executable_name
    executable.parent.mkdir(parents=True)
    executable.touch()

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout="nats-server: v2.14.6\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: system)
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: machine)

    command = build_nats_command(ProvisioningConfig(version="2.14.6", provider="github", cache_dir=tmp_path))

    assert command == (str(executable),)


def test_invalid_cache_entry_is_repaired_once(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    executable = tmp_path / version / "linux" / "amd64" / "nats-server"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"damaged")
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"repaired")
    digest = hashlib.sha256(archive).hexdigest()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        reported = "2.13.0" if command[0] == str(executable) else version
        return CompletedProcess(command, 0, stdout=f"nats-server: v{reported}\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    command = build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert command == (str(executable),)
    assert executable.read_bytes() == b"repaired"


def test_major_version_selector_is_accepted(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.15.0"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"server")
    digest = hashlib.sha256(archive).hexdigest()
    api_url = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            api_url: FakeResponse(payload=[{"tag_name": f"v{version}", "draft": False, "prerelease": False}]),
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    command = build_nats_command(ProvisioningConfig(version="2", provider="github", cache_dir=tmp_path))

    assert Path(command[0]) == tmp_path / version / "linux" / "amd64" / "nats-server"


def test_no_matching_partial_release_is_a_version_resolution_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    api_url = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
    client = FakeClient({api_url: FakeResponse(payload=[])})

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.12", provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.VERSION_RESOLUTION


def test_github_token_is_used_but_not_exposed_in_debug_logs(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    token = "secret-token-value"
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"server")
    digest = hashlib.sha256(archive).hexdigest()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )
    client_arguments: dict[str, object] = {}

    def client_factory(**kwargs: object) -> FakeClient:
        client_arguments.update(kwargs)
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")
    caplog.set_level(logging.DEBUG, logger="pytest_nats._provisioning")

    build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert client_arguments["headers"] == {"Authorization": f"Bearer {token}"}
    assert "GitHub" in caplog.text
    assert token not in caplog.text


def test_default_selector_uses_latest_and_auto_falls_back_to_github(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.16.0"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"server")
    digest = hashlib.sha256(archive).hexdigest()
    api_url = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            api_url: FakeResponse(payload=[{"tag_name": f"v{version}", "draft": False, "prerelease": False}]),
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    def which(_name: str) -> None:
        return None

    def cache_root(_application: str) -> Path:
        return tmp_path

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.shutil.which", which)
    monkeypatch.setattr("pytest_nats._provisioning.user_cache_path", cache_root)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    command = build_nats_command()

    assert command == (str(tmp_path / version / "linux" / "amd64" / "nats-server"),)
    assert client.requests[0] == api_url


def test_partial_selector_is_resolved_only_once_per_process(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.9.99"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"server")
    digest = hashlib.sha256(archive).hexdigest()
    api_url = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            api_url: FakeResponse(payload=[{"tag_name": f"v{version}", "draft": False, "prerelease": False}]),
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    build_nats_command(ProvisioningConfig(version="2.9", provider="github", cache_dir=tmp_path / "first"))
    build_nats_command(ProvisioningConfig(version="2.9", provider="github", cache_dir=tmp_path / "second"))

    assert client.requests.count(api_url) == 1


def test_darwin_arm64_uses_the_official_archive_name(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-darwin-arm64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-darwin-arm64/nats-server", b"server")
    digest = hashlib.sha256(archive).hexdigest()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "arm64")

    command = build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert command == (str(tmp_path / version / "macos" / "arm64" / "nats-server"),)
    assert client.requests[0].endswith(archive_name)


@mark.parametrize(("system", "machine"), [("FreeBSD", "x86_64"), ("Linux", "riscv64")])
def test_unsupported_managed_platform_is_a_configuration_error(
    system: str,
    machine: str,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: system)
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: machine)

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.14.6", provider="github"))

    assert raised.value.category is ErrorCategory.INVALID_CONFIGURATION


def test_github_token_is_used_for_release_discovery(
    monkeypatch: MonkeyPatch,
) -> None:
    token = "release-api-token"
    api_url = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
    client = FakeClient({api_url: FakeResponse(payload=[])})
    arguments: dict[str, object] = {}

    def client_factory(**kwargs: object) -> FakeClient:
        arguments.update(kwargs)
        return client

    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError):
        build_nats_command(ProvisioningConfig(version="2.7", provider="github"))

    assert arguments["headers"] == {"Authorization": f"Bearer {token}"}


def test_failed_cache_repair_removes_the_damaged_entry_and_temporary_files(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    executable = tmp_path / version / "linux" / "amd64" / "nats-server"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"damaged")
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"replacement")
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{'0' * 64}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout="nats-server: v2.13.0\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError):
        build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert list(executable.parent.iterdir()) == []


@mark.parametrize("checksum_kind", ["malformed", "missing"])
def test_invalid_checksum_file_blocks_publication(
    checksum_kind: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"server")
    digest = hashlib.sha256(archive).hexdigest()
    checksums = b"not a checksum file\n" if checksum_kind == "malformed" else f"{digest}  another-file\n".encode()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(checksums),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert list((tmp_path / version / "linux" / "amd64").iterdir()) == []


@mark.parametrize("archive_kind", ["malformed", "missing-member", "path-traversal"])
def test_unsafe_or_invalid_archive_blocks_publication(
    archive_kind: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    if archive_kind == "malformed":
        archive = b"not a tar archive"
    elif archive_kind == "missing-member":
        archive = make_tar_archive("unrelated/nats-server", b"server")
    else:
        archive = make_tar_archive("../../nats-server", b"server")
    digest = hashlib.sha256(archive).hexdigest()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert list((tmp_path / version / "linux" / "amd64").iterdir()) == []


def test_release_discovery_timeout_preserves_its_cause(monkeypatch: MonkeyPatch) -> None:
    timeout = TimeoutError("GitHub timed out")

    def client_factory(**_kwargs: object) -> None:
        raise timeout

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.6", provider="github"))

    assert raised.value.category is ErrorCategory.VERSION_RESOLUTION
    assert raised.value.__cause__ is timeout


def test_filesystem_failure_is_a_provisioning_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    failure = OSError("cache is read-only")

    def mkstemp(**_kwargs: object) -> tuple[int, str]:
        raise failure

    monkeypatch.setattr("pytest_nats._provisioning.tempfile.mkstemp", mkstemp)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.14.6", provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert raised.value.__cause__ is failure


def test_mise_timeout_is_a_provisioning_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    timeout = TimeoutExpired("mise", 300)

    def which(_name: str) -> str:
        return str(tmp_path / "mise")

    def run(_command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        raise timeout

    monkeypatch.setattr("pytest_nats._provisioning.shutil.which", which)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.14.6", provider="mise"))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert raised.value.__cause__ is timeout


def test_concurrent_downloads_publish_only_complete_cache_entries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"complete server")
    digest = hashlib.sha256(archive).hexdigest()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    responses = {
        f"{release_url}/{archive_name}": FakeResponse(archive),
        f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
    }
    both_workers_ready = Barrier(2)

    def client_factory(**_kwargs: object) -> FakeClient:
        both_workers_ready.wait(timeout=5)
        return FakeClient(responses)

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=f"nats-server: v{version}\n", stderr="")

    def provision() -> tuple[str, ...]:
        return build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with ThreadPoolExecutor(max_workers=2) as executor:
        commands = [executor.submit(provision), executor.submit(provision)]
        results = [future.result(timeout=5) for future in commands]

    executable = tmp_path / version / "linux" / "amd64" / "nats-server"
    assert results == [(str(executable),), (str(executable),)]
    assert executable.read_bytes() == b"complete server"
    assert list(executable.parent.iterdir()) == [executable]


@mark.parametrize(
    "version_output",
    ["not nats\n", "nats-server: v1.4.0\n", "nats-server: v2.01.0\n"],
)
def test_user_supplied_executable_rejects_invalid_identity(
    version_output: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout=version_output, stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(executable=tmp_path / "nats-server"))

    assert raised.value.category is ErrorCategory.PROVISIONING


def test_managed_executable_must_match_the_resolved_version(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    archive = make_tar_archive(f"nats-server-v{version}-linux-amd64/nats-server", b"wrong server")
    digest = hashlib.sha256(archive).hexdigest()
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient(
        {
            f"{release_url}/{archive_name}": FakeResponse(archive),
            f"{release_url}/SHA256SUMS": FakeResponse(f"{digest}  {archive_name}\n".encode()),
        }
    )

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout="nats-server: v2.14.5\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert list((tmp_path / version / "linux" / "amd64").iterdir()) == []


def test_http_status_error_while_downloading_preserves_its_cause(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    failure = RuntimeError("404 release asset not found")
    version = "2.14.6"
    archive_name = f"nats-server-v{version}-linux-amd64.tar.gz"
    release_url = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    client = FakeClient({f"{release_url}/{archive_name}": FakeResponse(status_error=failure)})

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version=version, provider="github", cache_dir=tmp_path))

    assert raised.value.category is ErrorCategory.PROVISIONING
    assert raised.value.__cause__ is failure


def test_invalid_release_api_payload_is_a_version_resolution_error(monkeypatch: MonkeyPatch) -> None:
    api_url = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
    client = FakeClient({api_url: FakeResponse(payload={"message": "unexpected"})})

    def client_factory(**_kwargs: object) -> FakeClient:
        return client

    monkeypatch.setattr("pytest_nats._provisioning.httpx2.Client", client_factory)
    monkeypatch.setattr("pytest_nats._provisioning.platform.system", lambda: "Linux")
    monkeypatch.setattr("pytest_nats._provisioning.platform.machine", lambda: "x86_64")

    with raises(AcquisitionError) as raised:
        build_nats_command(ProvisioningConfig(version="2.5", provider="github"))

    assert raised.value.category is ErrorCategory.VERSION_RESOLUTION
    assert isinstance(raised.value.__cause__, TypeError)


def test_successful_provisioning_writes_no_normal_output(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    executable = tmp_path / "nats-server"
    executable.touch()

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout="nats-server: v2.14.6\n", stderr="")

    monkeypatch.setattr("pytest_nats._provisioning.subprocess.run", run)

    build_nats_command(ProvisioningConfig(executable=executable))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
