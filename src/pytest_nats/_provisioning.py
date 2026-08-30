"""NATS executable selection and provisioning."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from typing import cast

import httpx2
from platformdirs import user_cache_path

_SEMANTIC_VERSION = (
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_NATS_VERSION_PATTERN = re.compile(rf"nats-server: v(?P<version>{_SEMANTIC_VERSION})")
_SELECTOR_PATTERN = re.compile(r"^(?:latest|(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))?(?:\.(?:0|[1-9]\d*))?)$")
_STABLE_VERSION_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_RELEASE_DOWNLOAD_URL = "https://github.com/nats-io/nats-server/releases/download"
_RELEASE_API_URL = "https://api.github.com/repos/nats-io/nats-server/releases"
_LOGGER = logging.getLogger(__name__)


class ExecutableErrorCategory(str, Enum):
    """Stable categories for NATS executable acquisition failures."""

    CONFIGURATION = "configuration"
    LOCAL = "local"
    VERSION_RESOLUTION = "version_resolution"
    PROVISIONING = "provisioning"


class NatsExecutableError(Exception):
    """Failure to acquire a NATS command."""

    def __init__(self, category: ExecutableErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


def _configuration_error(message: str) -> NatsExecutableError:
    return NatsExecutableError(ExecutableErrorCategory.CONFIGURATION, message)


def _local_value(value: object) -> str:
    if isinstance(value, str):
        executable = value
    elif isinstance(value, os.PathLike):
        executable = cast(os.PathLike[str] | os.PathLike[bytes], value).__fspath__()
    else:
        raise _configuration_error("Local executable must be a string or string-compatible path")
    if not isinstance(executable, str):
        raise _configuration_error("Local executable must be a string or string-compatible path")
    if executable == "" or Path(executable) == Path("."):
        raise _configuration_error("Local executable cannot be empty or the current directory")
    return executable


def _selector(value: object) -> str:
    if not isinstance(value, str) or _SELECTOR_PATTERN.fullmatch(value) is None:
        raise _configuration_error(f"invalid NATS version selector: {value!r}")
    if value != "latest" and value.split(".", 1)[0] != "2":
        raise _configuration_error("provisioning supports only NATS major version 2")
    if value in ("2.0", "2.1") or value.startswith(("2.0.", "2.1.")):
        raise _configuration_error("the selector cannot select a supported NATS release (>=2.2.0,<3)")
    return value


def _cache_value(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        path = value
    elif isinstance(value, os.PathLike):
        path = cast(os.PathLike[str] | os.PathLike[bytes], value).__fspath__()
    else:
        raise _configuration_error("cache_dir must be a string-compatible path")
    if not isinstance(path, str):
        raise _configuration_error("cache_dir must be a string-compatible path")
    return path


@dataclass(frozen=True, slots=True)
class Local:
    """Select a local NATS executable by command name or filesystem path."""

    executable: str = "nats-server"

    def __init__(self, executable: object = "nats-server") -> None:
        object.__setattr__(self, "executable", _local_value(executable))


@dataclass(frozen=True, slots=True)
class Provision:
    """Provision NATS with Mise when available, otherwise GitHub."""

    version: str = "latest"
    cache_dir: str | None = None

    def __init__(self, version: object = "latest", *, cache_dir: object | None = None) -> None:
        object.__setattr__(self, "version", _selector(version))
        object.__setattr__(self, "cache_dir", _cache_value(cache_dir))


@dataclass(frozen=True, slots=True)
class Mise:
    """Provision NATS through Mise."""

    version: str = "latest"

    def __init__(self, version: object = "latest") -> None:
        object.__setattr__(self, "version", _selector(version))


@dataclass(frozen=True, slots=True)
class GitHub:
    """Provision NATS from an official GitHub release."""

    version: str = "latest"
    cache_dir: str | None = None

    def __init__(self, version: object = "latest", *, cache_dir: object | None = None) -> None:
        object.__setattr__(self, "version", _selector(version))
        object.__setattr__(self, "cache_dir", _cache_value(cache_dir))


NatsExecutableSource = Local | Provision | Mise | GitHub


@dataclass(frozen=True, slots=True)
class AcquiredNats:
    """An acquired NATS command and its resolved NATS version."""

    command: tuple[str, ...]
    resolved_version: str


@dataclass(frozen=True, slots=True)
class _GitHubPlatform:
    cache_system: str
    cache_architecture: str
    archive_system: str
    archive_architecture: str

    @property
    def executable_name(self) -> str:
        return "nats-server.exe" if self.cache_system == "windows" else "nats-server"


def acquire_nats(source: NatsExecutableSource, root_path: Path) -> AcquiredNats:
    """Acquire one NATS command during fixture setup."""
    if isinstance(source, Local):
        return _acquire_local(source, root_path)
    if isinstance(source, Provision):
        mise = shutil.which("mise")
        if mise is not None:
            return _acquire_mise(Mise(source.version), Path(mise))
        return _acquire_github(GitHub(source.version, cache_dir=source.cache_dir), root_path)
    if isinstance(source, Mise):
        mise = shutil.which("mise")
        if mise is None:
            raise NatsExecutableError(
                ExecutableErrorCategory.PROVISIONING,
                "Mise was requested but the mise command is not available on PATH",
            )
        return _acquire_mise(source, Path(mise))
    return _acquire_github(source, root_path)


def _acquire_local(source: Local, root_path: Path) -> AcquiredNats:
    value = os.path.expanduser(source.executable)
    path = Path(value)
    try:
        if not path.is_absolute() and len(path.parts) == 1:
            found = shutil.which(value)
            if found is None:
                raise FileNotFoundError(f"command not found on PATH: {value}")
            path = Path(found)
        elif not path.is_absolute():
            path = root_path / path
        executable = _resolved_path(path)
        _validate_executable(executable)
        version = _local_executable_version(executable)
        if version.split(".", 1)[0] != "2":
            raise ValueError(f"expected a NATS 2.x executable, got {version}")
        return AcquiredNats((str(executable),), version)
    except Exception as exc:
        raise NatsExecutableError(
            ExecutableErrorCategory.LOCAL,
            f"failed to acquire local NATS executable {source.executable!r}",
        ) from exc


@cache
def _cached_mise(source: Mise, mise: Path) -> AcquiredNats:
    backend = f"github:nats-io/nats-server@{source.version}"
    try:
        subprocess.run([str(mise), "install", backend], check=True, capture_output=True, text=True, timeout=300)
        location = subprocess.run(
            [str(mise), "where", backend], check=True, capture_output=True, text=True, timeout=300
        )
        installation = _resolved_path(Path(location.stdout.strip()))
        executable = installation / _host_executable_name()
        _validate_executable(executable)
        resolved_version = (
            source.version if source.version.count(".") == 2 else _mise_resolved_version(mise, installation)
        )
        return AcquiredNats((str(executable),), resolved_version)
    except Exception as exc:
        details = ""
        if isinstance(exc, subprocess.CalledProcessError):
            output = (exc.stderr or exc.stdout or "").strip()
            details = f": {output}" if output else ""
        raise NatsExecutableError(
            ExecutableErrorCategory.PROVISIONING,
            f"Mise failed to provision NATS Server for selector {source.version!r}{details}",
        ) from exc


def _acquire_mise(source: Mise, mise: Path) -> AcquiredNats:
    return _cached_mise(source, _resolved_path(mise))


def _mise_resolved_version(mise: Path, installation: Path) -> str:
    listing = subprocess.run([str(mise), "ls", "--json"], check=True, capture_output=True, text=True, timeout=30)
    payload: object = json.loads(listing.stdout)
    if not isinstance(payload, dict):
        raise TypeError("Mise tool listing is not an object")
    for installations in cast(dict[object, object], payload).values():
        if not isinstance(installations, list):
            continue
        for item in cast(list[object], installations):
            if not isinstance(item, dict):
                continue
            record = cast(dict[object, object], item)
            version = record.get("version")
            install_path = record.get("install_path")
            if (
                isinstance(version, str)
                and isinstance(install_path, str)
                and _resolved_path(Path(install_path)) == installation
            ):
                return version
    raise ValueError(f"Mise did not report the resolved version for {installation}")


def _acquire_github(source: GitHub, root_path: Path) -> AcquiredNats:
    version = source.version if source.version.count(".") == 2 else _resolve_version(source.version)
    platform_value = _github_platform()
    cache_root = _cache_root(source.cache_dir, root_path)
    return AcquiredNats(_provision_from_github(version, platform_value, cache_root), version)


@cache
def _resolve_version(selector: str) -> str:
    matches: list[tuple[int, int, int]] = []
    try:
        with httpx2.Client(
            timeout=30,
            follow_redirects=True,
            headers=_github_headers(os.environ.get("GITHUB_TOKEN")),
        ) as client:
            page = 1
            while True:
                response = client.get(f"{_RELEASE_API_URL}?per_page=100&page={page}")
                response.raise_for_status()
                payload_object: object = response.json()
                if not isinstance(payload_object, list):
                    raise TypeError("GitHub release response is not a list")
                payload = cast(list[object], payload_object)
                for item in payload:
                    if not isinstance(item, dict):
                        raise TypeError("GitHub release response contains a non-object")
                    release = cast(dict[object, object], item)
                    if release.get("draft") is not False or release.get("prerelease") is not False:
                        continue
                    tag_name = release.get("tag_name")
                    if not isinstance(tag_name, str):
                        raise TypeError("GitHub release has no string tag_name")
                    match = _STABLE_VERSION_PATTERN.fullmatch(tag_name)
                    if match is None:
                        continue
                    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
                    if version >= (2, 2, 0) and version[0] == 2 and _selector_matches(selector, version):
                        matches.append(version)
                if len(payload) < 100:
                    break
                page += 1
    except Exception as exc:
        raise NatsExecutableError(
            ExecutableErrorCategory.VERSION_RESOLUTION,
            f"failed to resolve NATS version selector {selector!r}",
        ) from exc
    if not matches:
        raise NatsExecutableError(
            ExecutableErrorCategory.VERSION_RESOLUTION,
            f"no stable NATS 2.x release matches {selector!r}",
        )
    return ".".join(str(part) for part in max(matches))


def _selector_matches(selector: str, version: tuple[int, int, int]) -> bool:
    if selector == "latest":
        return True
    requested = tuple(int(part) for part in selector.split("."))
    return version[: len(requested)] == requested


def _github_platform() -> _GitHubPlatform:
    systems = {"Linux": ("linux", "linux"), "Darwin": ("macos", "darwin"), "Windows": ("windows", "windows")}
    architectures = {
        "x86_64": ("amd64", "amd64"),
        "AMD64": ("amd64", "amd64"),
        "aarch64": ("arm64", "arm64"),
        "arm64": ("arm64", "arm64"),
        "ARM64": ("arm64", "arm64"),
    }
    system_name = platform.system()
    machine_name = platform.machine()
    try:
        system, archive_system = systems[system_name]
        architecture, archive_architecture = architectures[machine_name]
    except KeyError as exc:
        raise NatsExecutableError(
            ExecutableErrorCategory.PROVISIONING,
            f"GitHub provisioning is unsupported on {system_name} {machine_name}",
        ) from exc
    return _GitHubPlatform(system, architecture, archive_system, archive_architecture)


def _cache_root(value: str | None, root_path: Path) -> Path:
    if value is None:
        return _resolved_path(user_cache_path("pytest-nats"))
    path = Path(os.path.expanduser(value))
    return _resolved_path(path if path.is_absolute() else root_path / path)


def _provision_from_github(version: str, target_platform: _GitHubPlatform, cache_root: Path) -> tuple[str, ...]:
    archive_path: Path | None = None
    executable_path: Path | None = None
    target = _resolved_path(
        cache_root
        / version
        / target_platform.cache_system
        / target_platform.cache_architecture
        / target_platform.executable_name
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_file() and os.access(target, os.X_OK):
                return (str(target),)
            if not target.is_dir():
                target.unlink()
        extension = "zip" if target_platform.cache_system == "windows" else "tar.gz"
        archive_name = f"nats-server-v{version}-{target_platform.archive_system}-{target_platform.archive_architecture}.{extension}"
        release_url = f"{_RELEASE_DOWNLOAD_URL}/v{version}"
        archive_path = _temporary_path(target.parent, ".archive-")
        executable_path = _temporary_path(target.parent, ".executable-")
        with httpx2.Client(
            timeout=30, follow_redirects=True, headers=_github_headers(os.environ.get("GITHUB_TOKEN"))
        ) as client:
            _download(client, f"{release_url}/{archive_name}", archive_path)
            checksum_response = client.get(f"{release_url}/SHA256SUMS")
            checksum_response.raise_for_status()
            _verify_checksum(archive_path, archive_name, checksum_response.content)
        member_name = (
            f"nats-server-v{version}-{target_platform.archive_system}-{target_platform.archive_architecture}/"
            f"{target_platform.executable_name}"
        )
        _extract_executable(archive_path, member_name, executable_path, extension)
        executable_path.chmod(executable_path.stat().st_mode | 0o755)
        _validate_executable(executable_path)
        _publish(executable_path, target)
        return (str(target),)
    except NatsExecutableError:
        raise
    except Exception as exc:
        raise NatsExecutableError(
            ExecutableErrorCategory.PROVISIONING,
            f"failed to provision NATS Server {version} from GitHub",
        ) from exc
    finally:
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)
        if executable_path is not None:
            executable_path.unlink(missing_ok=True)


def _host_executable_name() -> str:
    return "nats-server.exe" if os.name == "nt" else "nats-server"


def _validate_executable(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"NATS executable is not a regular file: {path}")
    if not os.access(path, os.X_OK):
        raise PermissionError(f"NATS executable is not executable: {path}")


def _local_executable_version(executable: Path) -> str:
    result = subprocess.run([str(executable), "--version"], check=True, capture_output=True, text=True, timeout=5)
    for line in (*result.stdout.splitlines(), *result.stderr.splitlines()):
        match = _NATS_VERSION_PATTERN.fullmatch(line.strip())
        if match is not None:
            return match.group("version")
    raise ValueError("unrecognized NATS Server version output")


def _github_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _temporary_path(directory: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix)
    os.close(descriptor)
    return Path(name)


def _resolved_path(path: Path) -> Path:
    resolved = path.resolve()
    text = str(resolved)
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[8:])
    if text.startswith("\\\\?\\"):
        return Path(text[4:])
    return resolved


def _publish(source: Path, target: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * 2**attempt)


def _download(client: httpx2.Client, url: str, destination: Path) -> None:
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with destination.open("wb") as stream:
            for chunk in response.iter_bytes():
                stream.write(chunk)


def _verify_checksum(archive_path: Path, archive_name: str, checksums: bytes) -> None:
    expected = None
    for line in checksums.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive_name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            expected = parts[0].lower()
            break
    if expected is None:
        raise ValueError(f"SHA256SUMS has no valid entry for {archive_name}")
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch for {archive_name}")


def _extract_executable(archive_path: Path, member_name: str, destination: Path, extension: str) -> None:
    if extension == "zip":
        with zipfile.ZipFile(archive_path) as archive:
            member = archive.getinfo(member_name)
            if member.is_dir():
                raise ValueError(f"archive member is not a file: {member_name}")
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
        return
    with tarfile.open(archive_path, mode="r:gz") as archive:
        member = archive.getmember(member_name)
        if not member.isfile():
            raise ValueError(f"archive member is not a file: {member_name}")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError(f"could not read archive member: {member_name}")
        with source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
