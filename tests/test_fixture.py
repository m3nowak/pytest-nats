import os
from pathlib import Path
from typing import Any, cast

from pytest import MonkeyPatch, Pytester, mark, raises

from pytest_nats import NatsExecutableError, nats_server_fixture


@mark.parametrize("scope", ["class", "package", "invalid"])
def test_fixture_rejects_unsupported_scope(scope: str) -> None:
    with raises(ValueError, match="scope"):
        nats_server_fixture(scope=cast(Any, scope))


@mark.parametrize(
    ("setting", "value"),
    [
        ("max_memory_store", 0),
        ("max_memory_store", -1),
        ("max_memory_store", True),
        ("max_file_store", 0),
        ("max_file_store", 1.5),
        ("startup_timeout", 0),
        ("startup_timeout", -1),
        ("startup_timeout", True),
    ],
)
def test_fixture_rejects_non_positive_resource_settings(setting: str, value: object) -> None:
    with raises(ValueError, match=setting):
        nats_server_fixture(**cast(dict[str, Any], {setting: value}))


@mark.parametrize("binary", ["nats-server", Path("nats-server")])
def test_fixture_rejects_raw_binary_values(binary: object) -> None:
    with raises(NatsExecutableError, match="Local"):
        nats_server_fixture(cast(Any, binary))


def test_fixture_defaults_to_local_lookup_and_reports_startup_diagnostics(
    pytester: Pytester, monkeypatch: MonkeyPatch
) -> None:
    executable = pytester.path / ("nats-server.cmd" if os.name == "nt" else "nats-server")
    source = (
        """@echo off
if "%1"=="--version" (
  echo nats-server: v2.12.15
  exit /b 0
)
echo controlled stdout
echo controlled stderr 1>&2
exit /b 7
"""
        if os.name == "nt"
        else """#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["--version"]:
    print("nats-server: v2.12.15")
    raise SystemExit(0)
print("controlled stdout", flush=True)
print("controlled stderr", file=sys.stderr, flush=True)
raise SystemExit(7)
"""
    )
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(pytester.path), os.environ.get("PATH", ""))))
    pytester.makeconftest(
        """
from pytest_nats import nats_server_fixture

failed_server = nats_server_fixture()
"""
    )
    pytester.makepyfile(
        """
def test_server(failed_server):
    raise AssertionError("fixture unexpectedly started")
"""
    )

    result = pytester.runpytest_subprocess("-q")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*NatsServerError: test NATS server exited during startup*",
            "*return code: 7*",
            "*controlled stdout*",
            "*controlled stderr*",
        ]
    )
