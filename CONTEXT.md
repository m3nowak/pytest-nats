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
An official stable release in the NATS 2.x line.
_Avoid_: Available version, GitHub tag

**NATS command**:
A launch command prefix for a provisioned or user-supplied `nats-server`, ready to be extended with runtime arguments.
_Avoid_: Binary path, shell command

**User-supplied NATS executable**:
An existing `nats-server` selected instead of requesting a version for provisioning. Its reported identity is validated before it becomes a NATS command.
_Avoid_: Custom version, local provider
