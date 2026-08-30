"""Private lifecycle implementation for test NATS server fixtures."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Literal, cast
from urllib.parse import urlparse

import pytest

from ._provisioning import (
    ExecutableErrorCategory,
    GitHub,
    Local,
    Mise,
    NatsExecutableError,
    NatsExecutableSource,
    Provision,
    acquire_nats,
)

FixtureScope = Literal["function", "module", "session"]
_HOST = "127.0.0.1"
_DEFAULT_MAX_MEMORY_STORE = 256 * 1024 * 1024
_DEFAULT_MAX_FILE_STORE = 1024 * 1024 * 1024
_SHUTDOWN_TIMEOUT = 5.0
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class NatsServerError(Exception):
    """A test NATS server startup or lifecycle failure."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        details = [message]
        if returncode is not None:
            details.append(f"return code: {returncode}")
        if stdout:
            details.append(f"stdout:\n{stdout}")
        if stderr:
            details.append(f"stderr:\n{stderr}")
        super().__init__("\n".join(details))
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _OutputCapture:
    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._lock = threading.Lock()

    def append(self, chunk: str) -> None:
        with self._lock:
            self._chunks.append(chunk)

    def snapshot(self) -> str:
        with self._lock:
            return "".join(self._chunks)


class NatsServer:
    """Read-only connection metadata and diagnostics for a test NATS server."""

    __slots__ = ("_host", "_jetstream_enabled", "_port", "_resolved_version", "_stderr", "_stdout")

    def __init__(
        self,
        *,
        host: str,
        port: int,
        resolved_version: str,
        jetstream_enabled: bool,
        stdout: _OutputCapture,
        stderr: _OutputCapture,
    ) -> None:
        self._host = host
        self._port = port
        self._resolved_version = resolved_version
        self._jetstream_enabled = jetstream_enabled
        self._stdout = stdout
        self._stderr = stderr

    @property
    def url(self) -> str:
        """Return the client connection URL."""
        return f"nats://{self._host}:{self._port}"

    @property
    def host(self) -> str:
        """Return the IPv4 loopback host."""
        return self._host

    @property
    def port(self) -> int:
        """Return the dynamically selected client port."""
        return self._port

    @property
    def resolved_version(self) -> str:
        """Return the resolved NATS version."""
        return self._resolved_version

    @property
    def jetstream_enabled(self) -> bool:
        """Return whether JetStream is enabled."""
        return self._jetstream_enabled

    @property
    def stdout(self) -> str:
        """Return all server standard output captured so far."""
        return self._stdout.snapshot()

    @property
    def stderr(self) -> str:
        """Return all server standard error captured so far."""
        return self._stderr.snapshot()


class _ServerProcess:
    def __init__(
        self,
        source: NatsExecutableSource,
        root_path: Path,
        *,
        jetstream: bool,
        max_memory_store: int,
        max_file_store: int,
        startup_timeout: float,
    ) -> None:
        self._source = source
        self._root_path = root_path
        self._jetstream = jetstream
        self._max_memory_store = max_memory_store
        self._max_file_store = max_file_store
        self._startup_timeout = startup_timeout
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._readers: list[threading.Thread] = []
        self._stdout = _OutputCapture()
        self._stderr = _OutputCapture()

    def __enter__(self) -> NatsServer:
        try:
            provisioned = acquire_nats(self._source, self._root_path)
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="pytest-nats-")
            temporary_path = Path(self._temporary_directory.name)
            config_path = temporary_path / "nats.conf"
            config_path.write_text(
                _server_config(
                    temporary_path / "jetstream",
                    self._jetstream,
                    self._max_memory_store,
                    self._max_file_store,
                ),
                encoding="utf-8",
            )
            self._process = subprocess.Popen(
                (*provisioned.command, "-c", str(config_path), "--ports_file_dir", str(temporary_path)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0,
            )
            self._start_readers()
            client_port = self._wait_until_ready(temporary_path)
            return NatsServer(
                host=_HOST,
                port=client_port,
                resolved_version=provisioned.resolved_version,
                jetstream_enabled=self._jetstream,
                stdout=self._stdout,
                stderr=self._stderr,
            )
        except NatsExecutableError:
            self._cleanup()
            raise
        except NatsServerError:
            self._stop()
            self._cleanup()
            raise
        except Exception as exc:
            self._stop()
            error = self._error("failed to start test NATS server")
            self._cleanup()
            raise error from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, exc, traceback
        process = self._process
        unexpected_returncode = process.poll() if process is not None else None
        self._stop()
        error = (
            self._error("test NATS server exited unexpectedly", returncode=unexpected_returncode)
            if unexpected_returncode is not None
            else None
        )
        self._cleanup()
        if error is not None:
            raise error
        return False

    def _start_readers(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        for stream, capture in ((self._process.stdout, self._stdout), (self._process.stderr, self._stderr)):
            reader = threading.Thread(target=_drain, args=(stream.readline, capture), daemon=True)
            reader.start()
            self._readers.append(reader)

    def _wait_until_ready(self, temporary_path: Path) -> int:
        deadline = time.monotonic() + self._startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            assert self._process is not None
            returncode = self._process.poll()
            if returncode is not None:
                self._join_readers()
                raise self._error("test NATS server exited during startup", returncode=returncode)
            try:
                client_port, monitor_port = _read_ports(temporary_path)
                _check_nats(client_port)
                _check_health(monitor_port, self._jetstream)
                return client_port
            except (OSError, TypeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
                last_error = exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise self._error(
            f"test NATS server did not become ready within {self._startup_timeout:g} seconds"
        ) from last_error

    def _stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            if os.name == "nt":
                process.send_signal(cast(int, getattr(signal, "CTRL_BREAK_EVENT")))  # noqa: B009 - Windows-only.
            else:
                process.terminate()
            try:
                process.wait(timeout=_SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self._join_readers()

    def _join_readers(self) -> None:
        for reader in self._readers:
            reader.join(timeout=1)

    def _cleanup(self) -> None:
        if self._temporary_directory is not None:
            for attempt in range(5):
                try:
                    self._temporary_directory.cleanup()
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * 2**attempt)
            self._temporary_directory = None

    def _error(self, message: str, *, returncode: int | None = None) -> NatsServerError:
        if returncode is None and self._process is not None:
            returncode = self._process.poll()
        return NatsServerError(
            message,
            returncode=returncode,
            stdout=self._stdout.snapshot(),
            stderr=self._stderr.snapshot(),
        )


def _drain(readline: Callable[[], str], capture: _OutputCapture) -> None:
    while chunk := readline():
        capture.append(chunk)


def _server_config(
    store_directory: Path,
    jetstream: bool,
    max_memory_store: int,
    max_file_store: int,
) -> str:
    lines = [
        f'host: "{_HOST}"',
        "port: -1",
        f'http: "{_HOST}:-1"',
    ]
    if jetstream:
        lines.extend(
            [
                "jetstream {",
                f"  store_dir: {json.dumps(str(store_directory))}",
                f"  max_mem: {max_memory_store}",
                f"  max_file: {max_file_store}",
                "}",
            ]
        )
    return "\n".join(lines) + "\n"


def _read_ports(directory: Path) -> tuple[int, int]:
    ports_files = list(directory.glob("*.ports"))
    if not ports_files:
        raise FileNotFoundError("NATS ports file is not available")
    payload: object = json.loads(ports_files[0].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("NATS ports file is not an object")
    ports = cast(dict[object, object], payload)
    return _port_from_urls(ports.get("nats")), _port_from_urls(ports.get("monitoring"))


def _port_from_urls(value: object) -> int:
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise ValueError("NATS ports file has no listener URL")
    parsed = urlparse(value[0])
    if parsed.hostname != _HOST or parsed.port is None:
        raise ValueError("NATS listener is not bound to IPv4 loopback")
    return parsed.port


def _check_nats(port: int) -> None:
    with socket.create_connection((_HOST, port), timeout=0.2) as connection:
        connection.settimeout(0.2)
        info = connection.recv(65536)
        if not info.startswith(b"INFO "):
            raise ValueError("NATS server did not send INFO")
        connection.sendall(b"PING\r\n")
        response = connection.recv(65536)
        if b"PONG\r\n" not in response:
            raise ValueError("NATS server did not answer PING")


def _check_health(port: int, jetstream: bool) -> None:
    query = "?js-enabled-only=true" if jetstream else ""
    with _NO_PROXY_OPENER.open(f"http://{_HOST}:{port}/healthz{query}", timeout=0.2) as response:
        payload: object = json.load(response)
    if not isinstance(payload, dict) or cast(dict[object, object], payload).get("status") != "ok":
        raise ValueError("NATS health endpoint did not report ok")


def nats_server_fixture(
    binary: NatsExecutableSource | None = None,
    *,
    scope: FixtureScope = "function",
    jetstream: bool = False,
    max_memory_store: int = _DEFAULT_MAX_MEMORY_STORE,
    max_file_store: int = _DEFAULT_MAX_FILE_STORE,
    startup_timeout: float = 10.0,
) -> Callable[[], Iterator[NatsServer]]:
    """Return a pytest fixture definition owning one test NATS server."""
    scope_value = cast(object, scope)
    if scope_value not in ("function", "module", "session"):
        raise ValueError(f"unsupported fixture scope: {scope!r}")
    if not _is_boolean(jetstream):
        raise ValueError("jetstream must be a boolean")
    for name, value in cast(
        tuple[tuple[str, object], ...],
        (("max_memory_store", max_memory_store), ("max_file_store", max_file_store)),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer byte count")
    if not _is_positive_number(startup_timeout):
        raise ValueError("startup_timeout must be a positive duration in seconds")
    if binary is None:
        source: NatsExecutableSource = Local()
    elif isinstance(cast(object, binary), (Local, Provision, Mise, GitHub)):
        source = binary
    else:
        raise NatsExecutableError(
            ExecutableErrorCategory.CONFIGURATION,
            "binary must be Local, Provision, Mise, GitHub, or None",
        )

    @pytest.fixture(scope=scope)
    def fixture_definition(request: pytest.FixtureRequest) -> Iterator[NatsServer]:
        with _ServerProcess(
            source,
            request.config.rootpath,
            jetstream=jetstream,
            max_memory_store=max_memory_store,
            max_file_store=max_file_store,
            startup_timeout=float(startup_timeout),
        ) as server:
            yield server

    return fixture_definition


def _is_boolean(value: object) -> bool:
    return isinstance(value, bool)


def _is_positive_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0
