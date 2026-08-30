# Declare NATS server fixtures with a factory

pytest-nats exposes `nats_server_fixture(...)`, which developers assign in `conftest.py` to declare fixtures with their chosen names, scopes, provisioning settings, and JetStream settings. The package does not auto-register a fixed pytest plugin fixture because explicit declarations preserve local control, allow multiple differently configured servers in one suite, and avoid global behavior on installation.
