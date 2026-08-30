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

## Provisioning

By default, `latest` is resolved with the `auto` provider policy. Stable NATS
releases from 2.2.0 up to, but not including, 3.0.0 are supported.

```python
from pathlib import Path

fixed_nats = nats_server_fixture(version="2.12.15")
major_minor_nats = nats_server_fixture(version="2.12", provider="github")
mise_nats = nats_server_fixture(version="2", provider="mise")
cached_nats = nats_server_fixture(cache_dir=Path(".cache/nats"))
local_nats = nats_server_fixture(executable=Path("/opt/nats/nats-server"))
```

Provider choices are `auto`, `mise`, and `github`. A user-supplied executable
cannot be combined with `version` or a managed provider. `startup_timeout`
sets the positive setup deadline in seconds and defaults to 10 seconds.

### mise

[mise](https://mise.jdx.dev/) is a development-tool version manager. When
`provider="mise"` is selected, pytest-nats asks the `mise` executable on `PATH`
to install and locate the resolved release through its
[GitHub backend](https://mise.jdx.dev/dev-tools/backends/github.html). The
`auto` policy also selects mise when it is available; otherwise it uses the
GitHub provider.

### GitHub

The `github` provider downloads official
[NATS Server releases](https://github.com/nats-io/nats-server/releases),
verifies their published checksums, and stores validated executables in the
selected cache directory. Set the optional `GITHUB_TOKEN` environment variable
to authenticate GitHub API and download requests, which can avoid anonymous API
rate limits. See GitHub's
[personal access token documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
for token creation and handling guidance.

Provisioning, startup, and lifecycle failures raise `NatsServerError`. Its
`returncode`, `stdout`, and `stderr` attributes retain available diagnostics;
the same diagnostics are included in the exception message.

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
