import os
from pathlib import Path
from typing import Any, cast

from pytest import Pytester, mark, raises

from pytest_nats import nats_server_fixture


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


def test_fixture_rejects_conflicting_provisioning_settings(tmp_path: Path) -> None:
    with raises(ValueError, match="executable"):
        nats_server_fixture(version="2.12.15", executable=tmp_path / "nats-server")


@mark.parametrize("version", ["2.1.9", "3.0.0"])
def test_fixture_rejects_unsupported_exact_releases(version: str) -> None:
    with raises(ValueError, match="supported NATS release|major version 2"):
        nats_server_fixture(version=version, provider="github")


def test_fixture_reports_startup_process_diagnostics(pytester: Pytester) -> None:
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
    pytester.makeconftest(
        f"""
from pytest_nats import nats_server_fixture

failed_server = nats_server_fixture(executable={str(executable)!r})
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
