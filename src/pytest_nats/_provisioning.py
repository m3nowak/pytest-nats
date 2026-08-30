"""Internal NATS executable provisioning."""

from __future__ import annotations

import hashlib
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
from typing import Literal, cast

import httpx2
from platformdirs import user_cache_path

_SEMANTIC_VERSION = (
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_NATS_VERSION_PATTERN = re.compile(rf"^nats-server: v(?P<version>{_SEMANTIC_VERSION})$")
_SELECTOR_PATTERN = re.compile(r"^(?:latest|(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))?(?:\.(?:0|[1-9]\d*))?)$")
_STABLE_VERSION_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_RELEASE_DOWNLOAD_URL = "https://github.com/nats-io/nats-server/releases/download"
_RELEASE_API_URL = "https://api.github.com/repos/nats-io/nats-server/releases"
_LOGGER = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    """Stable failure categories for future pytest integration."""

    INVALID_CONFIGURATION = "invalid_configuration"
    VERSION_RESOLUTION = "version_resolution"
    PROVISIONING = "provisioning"


class AcquisitionError(Exception):
    """Failure to build a validated NATS command."""

    def __init__(self, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ProvisioningConfig:
    """Inputs for building one validated NATS command."""

    version: str | None = None
    executable: str | Path | None = None
    provider: Literal["auto", "mise", "github"] = "auto"
    cache_dir: Path | None = None


@dataclass(frozen=True)
class _ManagedPlatform:
    cache_system: str
    cache_architecture: str
    archive_system: str
    archive_architecture: str

    @property
    def executable_name(self) -> str:
        return "nats-server.exe" if self.cache_system == "windows" else "nats-server"


def build_nats_command(config: ProvisioningConfig | None = None) -> tuple[str, ...]:
    """Return an immutable command containing a validated NATS executable."""
    config = config or ProvisioningConfig()
    if config.executable is not None and config.version is not None:
        raise AcquisitionError(
            ErrorCategory.INVALID_CONFIGURATION,
            "a user-supplied executable cannot be combined with a version selector",
        )
    if config.executable is not None and config.provider != "auto":
        raise AcquisitionError(
            ErrorCategory.INVALID_CONFIGURATION,
            "a user-supplied executable cannot be combined with a managed provider policy",
        )
    if config.executable is not None:
        executable = Path(config.executable).resolve()
        try:
            version = _executable_version(executable)
        except Exception as exc:
            raise AcquisitionError(
                ErrorCategory.PROVISIONING,
                f"failed to validate user-supplied NATS executable: {executable}",
            ) from exc
        if not version.startswith("2."):
            raise AcquisitionError(
                ErrorCategory.PROVISIONING,
                "the user-supplied executable is not NATS Server 2.x",
            )
        return (str(executable),)

    selector = _validated_selector(config.version if config.version is not None else "latest")
    if selector != "latest" and selector.split(".", 1)[0] != "2":
        raise AcquisitionError(
            ErrorCategory.INVALID_CONFIGURATION,
            "managed NATS provisioning supports only major version 2",
        )
    if config.provider not in ("auto", "mise", "github"):
        raise AcquisitionError(
            ErrorCategory.INVALID_CONFIGURATION,
            f"invalid managed provider policy: {config.provider!r}",
        )
    managed_platform = _managed_platform()
    version = selector if selector.count(".") == 2 else _resolve_version(selector)
    mise = shutil.which("mise")
    if config.provider == "mise" and mise is None:
        raise AcquisitionError(
            ErrorCategory.PROVISIONING,
            "the mise provider was requested but mise is not available on PATH",
        )
    if config.provider == "mise" or (config.provider == "auto" and mise is not None):
        assert mise is not None
        _LOGGER.debug("Selected mise to provision NATS Server %s", version)
        return _provision_from_mise(Path(mise).resolve(), version, managed_platform)
    _LOGGER.debug("Selected GitHub to provision NATS Server %s", version)
    return _provision_from_github(
        version,
        managed_platform,
        config.cache_dir,
    )


def _validated_selector(selector: object) -> str:
    if not isinstance(selector, str) or _SELECTOR_PATTERN.fullmatch(selector) is None:
        raise AcquisitionError(
            ErrorCategory.INVALID_CONFIGURATION,
            f"invalid managed NATS version selector: {selector!r}",
        )
    return selector


def _managed_platform() -> _ManagedPlatform:
    systems = {
        "Linux": ("linux", "linux"),
        "Darwin": ("macos", "darwin"),
        "Windows": ("windows", "windows"),
    }
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
        raise AcquisitionError(
            ErrorCategory.INVALID_CONFIGURATION,
            f"managed NATS provisioning is unsupported on {system_name} {machine_name}",
        ) from exc
    return _ManagedPlatform(system, architecture, archive_system, archive_architecture)


def _provision_from_mise(mise: Path, version: str, managed_platform: _ManagedPlatform) -> tuple[str, ...]:
    backend = f"github:nats-io/nats-server@{version}"
    try:
        install = subprocess.run(
            [str(mise), "install", backend],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        _LOGGER.debug("mise install completed: %s", install.stdout.strip())
        location = subprocess.run(
            [str(mise), "where", backend],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        _LOGGER.debug("mise reported installation directory %s", location.stdout.strip())
        executable = (Path(location.stdout.strip()) / managed_platform.executable_name).resolve()
        actual_version = _executable_version(executable)
        if actual_version != version:
            raise ValueError(f"expected NATS Server {version}, got {actual_version}")
    except Exception as exc:
        details = ""
        if isinstance(exc, subprocess.CalledProcessError):
            output = (exc.stderr or exc.stdout or "").strip()
            details = f": {output}" if output else ""
        raise AcquisitionError(
            ErrorCategory.PROVISIONING,
            f"mise failed to provision NATS Server {version}{details}",
        ) from exc
    return (str(executable),)


@cache
def _resolve_version(selector: str) -> str:
    matches: list[tuple[int, int, int]] = []
    _LOGGER.debug("Resolving NATS version selector %s through GitHub", selector)
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
                    if version[0] != 2 or not _selector_matches(selector, version):
                        continue
                    matches.append(version)
                if len(payload) < 100:
                    break
                page += 1
    except Exception as exc:
        raise AcquisitionError(
            ErrorCategory.VERSION_RESOLUTION,
            f"failed to resolve NATS version selector {selector!r}",
        ) from exc
    if not matches:
        raise AcquisitionError(
            ErrorCategory.VERSION_RESOLUTION,
            f"no stable NATS 2.x release matches {selector!r}",
        )
    resolved = ".".join(str(part) for part in max(matches))
    _LOGGER.debug("Resolved NATS version selector %s to %s", selector, resolved)
    return resolved


def _selector_matches(selector: str, version: tuple[int, int, int]) -> bool:
    if selector == "latest":
        return True
    requested = tuple(int(part) for part in selector.split("."))
    return version[: len(requested)] == requested


def _github_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _provision_from_github(
    version: str,
    managed_platform: _ManagedPlatform,
    cache_dir: Path | None,
) -> tuple[str, ...]:
    archive_path: Path | None = None
    executable_path: Path | None = None
    try:
        target = (
            (cache_dir or user_cache_path("pytest-nats"))
            / version
            / managed_platform.cache_system
            / managed_platform.cache_architecture
            / managed_platform.executable_name
        )
        target = _resolved_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                if _executable_version(target) == version:
                    _LOGGER.debug("Using validated GitHub cache entry %s", target)
                    return (str(target),)
            except Exception:
                _LOGGER.debug("Discarding invalid GitHub cache entry %s", target, exc_info=True)
            target.unlink(missing_ok=True)
        extension = "zip" if managed_platform.cache_system == "windows" else "tar.gz"
        archive_name = f"nats-server-v{version}-{managed_platform.archive_system}-{managed_platform.archive_architecture}.{extension}"
        release_url = f"{_RELEASE_DOWNLOAD_URL}/v{version}"
        headers = _github_headers(os.environ.get("GITHUB_TOKEN"))

        archive_path = _temporary_path(target.parent, ".archive-")
        executable_path = _temporary_path(target.parent, ".executable-")
        with httpx2.Client(timeout=30, follow_redirects=True, headers=headers) as client:
            _LOGGER.debug("Downloading official NATS archive %s", archive_name)
            _download(client, f"{release_url}/{archive_name}", archive_path)
            checksum_response = client.get(f"{release_url}/SHA256SUMS")
            checksum_response.raise_for_status()
            _verify_checksum(archive_path, archive_name, checksum_response.content)
        member_name = (
            f"nats-server-v{version}-{managed_platform.archive_system}-{managed_platform.archive_architecture}/"
            f"{managed_platform.executable_name}"
        )
        _extract_executable(archive_path, member_name, executable_path, extension)
        executable_path.chmod(executable_path.stat().st_mode | 0o755)
        actual_version = _executable_version(executable_path)
        if actual_version != version:
            raise ValueError(f"expected NATS Server {version}, got {actual_version}")
        _publish(executable_path, target)
        _LOGGER.debug("Published validated GitHub cache entry %s", target)
        return (str(target),)
    except AcquisitionError:
        raise
    except Exception as exc:
        raise AcquisitionError(
            ErrorCategory.PROVISIONING,
            f"failed to provision NATS Server {version} from GitHub",
        ) from exc
    finally:
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)
        if executable_path is not None:
            executable_path.unlink(missing_ok=True)


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


def _executable_version(executable: Path) -> str:
    _LOGGER.debug("Validating NATS executable identity at %s", executable)
    result = subprocess.run(
        [str(executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    match = _NATS_VERSION_PATTERN.fullmatch(result.stdout.rstrip("\r\n"))
    if match is None:
        raise ValueError("unrecognized NATS Server version output")
    return match.group("version")
