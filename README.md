# pytest-nats

Pytest helpers for starting isolated test NATS servers.

## Installation

```shell
pip install pytest-nats
```

pytest-nats does not register a global pytest plugin. Declare each server fixture
explicitly in `conftest.py`; the variable name becomes the fixture name:

```python
from pytest_nats import nats_server_fixture

nats_server = nats_server_fixture()
```

Tests receive a read-only `NatsServer` with the client URL, host, dynamic port,
resolved NATS version, JetStream state, and live `stdout` and `stderr` snapshots:

```python
from pytest_nats import NatsServer


def test_messaging(nats_server: NatsServer) -> None:
    assert nats_server.url == f"nats://127.0.0.1:{nats_server.port}"
    assert not nats_server.jetstream_enabled
```

The fixture binds its unauthenticated client and internal monitoring listeners
only to `127.0.0.1`, selects dynamic ports, and waits for both a NATS protocol
exchange and the health endpoint before yielding. It terminates the server and
removes generated configuration and data at the end of the selected scope.

## JetStream

Enable JetStream when declaring the fixture. One server supports both memory-
and file-backed streams; client code remains responsible for creating streams
and consumers.

```python
from pytest_nats import nats_server_fixture

jetstream_server = nats_server_fixture(
    jetstream=True,
    max_memory_store=512 * 1024 * 1024,
    max_file_store=2 * 1024 * 1024 * 1024,
)
```

The default aggregate limits are 256 MiB of memory and 1 GiB of file storage.
File data is isolated per server and removed during teardown.

## Fixture Scope

Function scope is the default. Module and session scopes retain server and
JetStream state for their normal pytest lifetime:

```python
module_nats = nats_server_fixture(scope="module")
session_nats = nats_server_fixture(scope="session", jetstream=True)
```

Supported scopes are `function`, `module`, and `session`.

## NATS executable selection

By default, pytest-nats finds `nats-server` on setup-time `PATH` and validates
that it reports a NATS 2.x semantic version. Use `Local` to select another
command name or path. Relative paths containing a directory are resolved from
pytest's root path.

```python
from pathlib import Path

from pytest_nats import GitHub, Local, Mise, Provision, nats_server_fixture

default_local_nats = nats_server_fixture()
alternate_local_nats = nats_server_fixture(Local("nats-server-another"))
local_path_nats = nats_server_fixture(Local(Path("tools/nats-server")))
latest_nats = nats_server_fixture(Provision())
mise_nats = nats_server_fixture(Mise("2.12"))
github_nats = nats_server_fixture(GitHub("2.12.15", cache_dir=Path(".cache/nats")))
```

`Local`, `Provision`, `Mise`, and `GitHub` are immutable source values. Raw
strings and paths are not accepted as the fixture's `binary` argument.
`Provision` prefers Mise when it is available on setup-time `PATH` and uses
GitHub otherwise. Automatic provisioning accepts `latest`, major, major-minor,
and exact stable NATS 2.x selectors that can select releases starting at 2.2.0.
`startup_timeout` sets the positive setup deadline in seconds and defaults to
10 seconds.

### mise

[mise](https://mise.jdx.dev/) is a development-tool version manager. `Mise`
asks the `mise` executable on `PATH` to install and locate the selector through its
[GitHub backend](https://mise.jdx.dev/dev-tools/backends/github.html). The
selector is passed directly to Mise. Successful acquisition is reused for the
rest of the pytest process, while failures remain retryable.

### GitHub

`GitHub` downloads official
[NATS Server releases](https://github.com/nats-io/nats-server/releases),
verifies their published checksums, and atomically stores executables in the
selected cache directory. Existing regular executable cache entries are trusted
without running or rehashing them. Set the optional `GITHUB_TOKEN` environment variable
to authenticate GitHub API and download requests, which can avoid anonymous API
rate limits. See GitHub's
[personal access token documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
for token creation and handling guidance.

Executable lookup, version resolution, and provisioning failures raise
`NatsExecutableError`. Its `category` is an `ExecutableErrorCategory` value.
Server startup and lifecycle failures raise `NatsServerError`; its `returncode`,
`stdout`, and `stderr` attributes retain available diagnostics.

## Development

Install [mise](https://mise.jdx.dev/getting-started.html), then install the project
tools and dependencies:

```shell
mise run install
mise run setup
```

Run tests and checks:

```shell
mise run test
mise run check
```

Apply automatic lint and formatting fixes:

```shell
mise run fix
```

## Releasing

Releases run from `.github/workflows/release.yml`. The workflow accepts a stable,
canonical PEP 440 version, runs the complete CI workflow, builds and validates
the source distribution and wheel, publishes them to PyPI, then publishes the
draft GitHub Release. Run it from the `main` branch in the GitHub Actions UI.
The first intended version is `0.0.1`, which creates tag `v0.0.1`.

Before the first release, create a pending Trusted Publisher on PyPI with these
values:

- PyPI project: `pytest-nats`
- GitHub owner: `m3nowak`
- GitHub repository: `pytest-nats`
- Workflow filename: `release.yml`
- Environment: `pypi`

Create the `pypi` environment in the GitHub repository without required
reviewers. The workflow requests an OIDC token only in the PyPI publication job;
no PyPI API token is needed.
