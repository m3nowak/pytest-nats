# pytest-nats

pytest-nats provides test suites with an on-demand NATS server whose release can be selected at different levels of precision.

## Language

**Version selector**:
A textual request for a stable NATS release: `latest`, a major version, a major-minor version, or an exact semantic version.
_Avoid_: Version, version number, version constraint

**Resolved NATS version**:
The exact semantic version of the stable NATS release selected by a version selector.
_Avoid_: Requested version, selected version

**Supported NATS release**:
A stable NATS 2.x release, starting with NATS 2.2.0, that can be selected for automatic provisioning. This range does not constrain a user-supplied NATS executable.
_Avoid_: Available version, GitHub tag

**NATS command**:
A launch command prefix for a provisioned or local `nats-server`, ready to be extended with runtime arguments.
_Avoid_: Binary path, shell command

**Local NATS executable**:
An existing NATS 2.x executable found by command name or selected by filesystem path instead of being provisioned. Its reported identity is validated before it becomes a NATS command.
_Avoid_: User-supplied executable, custom version, local provider

**Test NATS server**:
An isolated, unauthenticated `nats-server` instance created for tests and owned by a pytest fixture for the fixture's selected scope. It can provide Core NATS alone or Core NATS with JetStream.
_Avoid_: Ad hoc server, NATS instance
