import hashlib
import io
import json
import os
import platform
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import respx
from pytest import MonkeyPatch, fixture, mark, raises, skip

from pytest_nats import ExecutableErrorCategory, GitHub, Local, Mise, NatsExecutableError, Provision
from pytest_nats._provisioning import NatsExecutableSource, acquire_nats

_RELEASES_API = "https://api.github.com/repos/nats-io/nats-server/releases?per_page=100&page=1"
_DOWNLOAD_ROOT = "https://github.com/nats-io/nats-server/releases/download"


def _local_versions() -> dict[str, str]:
    return {}


def _calls() -> list[list[str]]:
    return []


def _command(source: NatsExecutableSource, root_path: Path) -> tuple[str, ...]:
    return acquire_nats(source, root_path).command


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
    local_versions: dict[str, str] = field(default_factory=_local_versions)
    calls: list[list[str]] = field(default_factory=_calls)
    mise_failure: Exception | None = None

    @property
    def executable(self) -> Path:
        return self.installation / _platform_names()[4]

    def run(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[-1] == "--version":
            version = self.local_versions.get(command[0], "2.14.6")
            return subprocess.CompletedProcess(
                command, 0, stdout=f"diagnostic\n  nats-server: v{version}  \n", stderr=""
            )
        if command[1] == "install":
            if self.mise_failure is not None:
                raise self.mise_failure
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1] == "ls":
            payload = {"github:nats-io/nats-server": [{"version": "2.14.6", "install_path": str(self.installation)}]}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        self.installation.mkdir(parents=True, exist_ok=True)
        self.executable.touch()
        self.executable.chmod(0o755)
        return subprocess.CompletedProcess(command, 0, stdout=f"{self.installation}\n", stderr="")


@fixture
def processes(tmp_path: Path, monkeypatch: MonkeyPatch) -> ProcessHarness:
    harness = ProcessHarness(tmp_path / "mise-installation" / "2.14.6")
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
        version, archive_name, archive_buffer.getvalue(), cache_system, cache_architecture, executable_name
    )


def _mock_download(httpx2_mock: respx.Router, artifact: ReleaseArtifact) -> tuple[respx.Route, respx.Route]:
    release_url = f"{_DOWNLOAD_ROOT}/v{artifact.version}"
    archive_route = httpx2_mock.get(f"{release_url}/{artifact.archive_name}").respond(content=artifact.archive)
    checksum = hashlib.sha256(artifact.archive).hexdigest()
    checksum_route = httpx2_mock.get(f"{release_url}/SHA256SUMS").respond(
        content=f"{checksum}  {artifact.archive_name}\n".encode()
    )
    return archive_route, checksum_route


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    path.chmod(0o755)
    return path


def _command_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _put_mise_on_path(directory: Path, monkeypatch: MonkeyPatch) -> None:
    directory.mkdir()
    _executable(directory / ("mise.exe" if os.name == "nt" else "mise"))
    monkeypatch.setenv("PATH", str(directory))


def test_local_command_is_looked_up_and_validated_on_every_request(
    tmp_path: Path, processes: ProcessHarness, monkeypatch: MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    executable = _executable(bin_dir / _command_name("nats-server"))
    monkeypatch.setenv("PATH", str(bin_dir))

    first = _command(Local(), tmp_path)
    second = _command(Local(), tmp_path)

    assert first == second == (str(executable.resolve()),)
    assert processes.calls == [[str(executable.resolve()), "--version"]] * 2


def test_local_relative_path_is_rooted_at_pytest_root(tmp_path: Path, processes: ProcessHarness) -> None:
    executable = _executable(tmp_path / "tools" / "nats-server")

    assert _command(Local("tools/nats-server"), tmp_path) == (str(executable.resolve()),)


def test_local_custom_command_name_is_searched_on_path(
    tmp_path: Path, processes: ProcessHarness, monkeypatch: MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    executable = _executable(bin_dir / _command_name("nats-server-another"))
    monkeypatch.setenv("PATH", str(bin_dir))

    assert _command(Local("nats-server-another"), tmp_path) == (str(executable.resolve()),)


def test_local_expands_home_but_not_environment_variables(
    tmp_path: Path, processes: ProcessHarness, monkeypatch: MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home_executable = _executable(home / "bin" / "nats-server")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("NATS_HOME", str(home))
    literal_executable = _executable(tmp_path / "$NATS_HOME" / "nats-server")

    assert _command(Local("~/bin/nats-server"), tmp_path) == (str(home_executable.resolve()),)
    assert _command(Local("$NATS_HOME/nats-server"), tmp_path) == (str(literal_executable.resolve()),)


def test_local_resolves_symlinks(tmp_path: Path, processes: ProcessHarness) -> None:
    executable = _executable(tmp_path / "real" / "nats-server")
    link = tmp_path / "linked-nats-server"
    try:
        link.symlink_to(executable)
    except OSError:
        skip("symlinks are unavailable")

    assert _command(Local(link), tmp_path) == (str(executable.resolve()),)


@mark.parametrize("version", ["2.0.0", "2.1.9", "2.14.6", "2.15.0-rc.1+build.4"])
def test_local_accepts_any_semantic_nats_2_version(version: str, tmp_path: Path, processes: ProcessHarness) -> None:
    executable = _executable(tmp_path / version / "nats-server")
    processes.local_versions[str(executable.resolve())] = version

    assert _command(Local(executable), tmp_path) == (str(executable.resolve()),)


def test_local_failure_has_a_stable_category_and_cause(tmp_path: Path) -> None:
    with raises(NatsExecutableError) as raised:
        _command(Local("missing-nats-server"), tmp_path)

    assert raised.value.category is ExecutableErrorCategory.LOCAL
    assert isinstance(raised.value.__cause__, FileNotFoundError)


@mark.parametrize("target_kind", ["directory", "non-executable", "malformed-version", "wrong-major"])
def test_local_rejects_invalid_targets(target_kind: str, tmp_path: Path, processes: ProcessHarness) -> None:
    if os.name == "nt" and target_kind == "non-executable":
        skip("Windows does not expose POSIX execute bits")
    target = tmp_path / target_kind / "nats-server"
    if target_kind == "directory":
        target.mkdir(parents=True)
    else:
        _executable(target)
    if target_kind == "non-executable":
        target.chmod(0o644)
    elif target_kind == "malformed-version":
        processes.local_versions[str(target.resolve())] = "not-a-version"
    elif target_kind == "wrong-major":
        processes.local_versions[str(target.resolve())] = "1.4.0"

    with raises(NatsExecutableError) as raised:
        _command(Local(target), tmp_path)

    assert raised.value.category is ExecutableErrorCategory.LOCAL


def test_github_provisions_an_exact_release_and_trusts_the_cache(tmp_path: Path, httpx2_mock: respx.Router) -> None:
    artifact = _artifact("2.14.6")
    archive_route, checksum_route = _mock_download(httpx2_mock, artifact)

    first = _command(GitHub(artifact.version, cache_dir=tmp_path), tmp_path)
    executable = artifact.executable(tmp_path)
    executable.write_bytes(b"externally changed")
    second = _command(GitHub(artifact.version, cache_dir=tmp_path), tmp_path)

    assert first == second == (str(executable.resolve()),)
    assert executable.read_bytes() == b"externally changed"
    assert archive_route.call_count == checksum_route.call_count == 1


def test_github_replaces_a_non_executable_cache_file(
    tmp_path: Path, httpx2_mock: respx.Router, monkeypatch: MonkeyPatch
) -> None:
    artifact = _artifact("2.14.6", b"fresh server")
    executable = artifact.executable(tmp_path)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"invalid cache entry")
    real_access = os.access

    def cache_access(path: str | os.PathLike[str], mode: int) -> bool:
        return False if Path(path) == executable else real_access(path, mode)

    monkeypatch.setattr(
        "pytest_nats._provisioning.os.access",
        cache_access,
    )
    _mock_download(httpx2_mock, artifact)

    assert _command(GitHub(artifact.version, cache_dir=tmp_path), tmp_path) == (str(executable),)
    assert executable.read_bytes() == b"fresh server"


@mark.parametrize(("selector", "resolved_version"), [("latest", "2.15.0"), ("2", "2.15.0"), ("2.14", "2.14.10")])
def test_github_resolves_stable_version_selectors(
    selector: str,
    resolved_version: str,
    tmp_path: Path,
    httpx2_mock: respx.Router,
    monkeypatch: MonkeyPatch,
) -> None:
    artifact = _artifact(resolved_version)
    api_route = httpx2_mock.get(_RELEASES_API).respond(
        json=[
            {"tag_name": "v2.14.9", "draft": False, "prerelease": False},
            {"tag_name": "v2.14.10", "draft": False, "prerelease": False},
            {"tag_name": "v2.15.0", "draft": False, "prerelease": False},
            {"tag_name": "v2.16.0", "draft": True, "prerelease": False},
        ]
    )
    _mock_download(httpx2_mock, artifact)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert _command(GitHub(selector, cache_dir=tmp_path), tmp_path) == (str(artifact.executable(tmp_path)),)
    assert api_route.calls.last is not None
    assert api_route.calls.last.request.headers["authorization"] == "Bearer test-token"


def test_relative_github_cache_is_rooted_at_pytest_root(tmp_path: Path, httpx2_mock: respx.Router) -> None:
    artifact = _artifact("2.14.6")
    _mock_download(httpx2_mock, artifact)

    command = _command(GitHub(artifact.version, cache_dir="cache"), tmp_path)

    assert command == (str(artifact.executable(tmp_path / "cache")),)


def test_github_expands_home_in_cache_root(tmp_path: Path, httpx2_mock: respx.Router, monkeypatch: MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    artifact = _artifact("2.14.6")
    _mock_download(httpx2_mock, artifact)

    assert _command(GitHub(artifact.version, cache_dir="~/nats-cache"), tmp_path) == (
        str(artifact.executable(home / "nats-cache")),
    )


def test_github_resolution_failure_has_a_stable_category(tmp_path: Path, httpx2_mock: respx.Router) -> None:
    httpx2_mock.get(_RELEASES_API).respond(json=[])

    with raises(NatsExecutableError) as raised:
        _command(GitHub("2.13", cache_dir=tmp_path), tmp_path)

    assert raised.value.category is ExecutableErrorCategory.VERSION_RESOLUTION


def test_successful_github_resolution_is_cached(tmp_path: Path, httpx2_mock: respx.Router) -> None:
    artifact = _artifact("2.10.9")
    api_route = httpx2_mock.get(_RELEASES_API).respond(
        json=[{"tag_name": "v2.10.9", "draft": False, "prerelease": False}]
    )
    _mock_download(httpx2_mock, artifact)

    first = _command(GitHub("2.10", cache_dir=tmp_path), tmp_path)
    second = _command(GitHub("2.10", cache_dir=tmp_path), tmp_path)

    assert first == second == (str(artifact.executable(tmp_path)),)
    assert api_route.call_count == 1


def test_github_does_not_publish_an_archive_with_a_bad_checksum(tmp_path: Path, httpx2_mock: respx.Router) -> None:
    artifact = _artifact("2.14.6")
    release_url = f"{_DOWNLOAD_ROOT}/v{artifact.version}"
    httpx2_mock.get(f"{release_url}/{artifact.archive_name}").respond(content=artifact.archive)
    httpx2_mock.get(f"{release_url}/SHA256SUMS").respond(content=f"{'0' * 64}  {artifact.archive_name}\n".encode())

    with raises(NatsExecutableError) as raised:
        _command(GitHub(artifact.version, cache_dir=tmp_path), tmp_path)

    assert raised.value.category is ExecutableErrorCategory.PROVISIONING
    assert not artifact.executable(tmp_path).exists()


def test_provision_prefers_mise_and_passes_fuzzy_selector_directly(
    tmp_path: Path, processes: ProcessHarness, httpx2_mock: respx.Router, monkeypatch: MonkeyPatch
) -> None:
    _put_mise_on_path(tmp_path / "bin", monkeypatch)

    acquired = acquire_nats(Provision("2.14"), tmp_path)

    assert acquired.command == (str(processes.executable.resolve()),)
    assert acquired.resolved_version == "2.14.6"
    assert processes.calls[0][-1] == "github:nats-io/nats-server@2.14"
    assert not httpx2_mock.calls.called


def test_provision_uses_github_when_mise_is_absent(
    tmp_path: Path, httpx2_mock: respx.Router, monkeypatch: MonkeyPatch
) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    artifact = _artifact("2.14.6")
    _mock_download(httpx2_mock, artifact)

    assert _command(Provision(artifact.version, cache_dir=tmp_path), tmp_path) == (str(artifact.executable(tmp_path)),)


def test_provision_does_not_fall_back_after_mise_failure(
    tmp_path: Path, processes: ProcessHarness, httpx2_mock: respx.Router, monkeypatch: MonkeyPatch
) -> None:
    _put_mise_on_path(tmp_path / "bin", monkeypatch)
    processes.mise_failure = subprocess.CalledProcessError(1, ["mise", "install"], stderr="failed")

    with raises(NatsExecutableError):
        _command(Provision("2.14.6", cache_dir=tmp_path), tmp_path)

    assert not httpx2_mock.calls.called


def test_forced_mise_reports_a_missing_command(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    with raises(NatsExecutableError) as raised:
        _command(Mise(), tmp_path)

    assert raised.value.category is ExecutableErrorCategory.PROVISIONING


def test_successful_mise_acquisition_is_cached(
    tmp_path: Path, processes: ProcessHarness, monkeypatch: MonkeyPatch
) -> None:
    _put_mise_on_path(tmp_path / "bin", monkeypatch)

    first = _command(Mise("2.14.6"), tmp_path)
    second = _command(Mise("2.14.6"), tmp_path)

    assert first == second
    assert len(processes.calls) == 2


def test_failed_mise_acquisition_is_retryable(
    tmp_path: Path, processes: ProcessHarness, monkeypatch: MonkeyPatch
) -> None:
    _put_mise_on_path(tmp_path / "bin", monkeypatch)
    failure = subprocess.CalledProcessError(1, ["mise", "install"], stderr="mise failed")
    processes.mise_failure = failure

    with raises(NatsExecutableError) as first:
        _command(Mise("2.12.15"), tmp_path)
    processes.mise_failure = None
    command = _command(Mise("2.12.15"), tmp_path)

    assert first.value.category is ExecutableErrorCategory.PROVISIONING
    assert first.value.__cause__ is failure
    assert command == (str(processes.executable.resolve()),)
