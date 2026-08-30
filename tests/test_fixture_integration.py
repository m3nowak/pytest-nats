# pyright: reportUnknownMemberType=false

import asyncio
import os
import socket

import nats
import pytest
from nats.js.api import StorageType, StreamConfig

from pytest_nats import GitHub, NatsServer, nats_server_fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PYTEST_NATS_RUN_INTEGRATION") != "1",
        reason="set PYTEST_NATS_RUN_INTEGRATION=1 to run real NATS server tests",
    ),
]

_INTEGRATION_VERSION = "2.12.15"

core_nats = nats_server_fixture(GitHub(_INTEGRATION_VERSION), scope="module")
jetstream_nats = nats_server_fixture(GitHub(_INTEGRATION_VERSION), jetstream=True, scope="module")
function_nats = nats_server_fixture(GitHub(_INTEGRATION_VERSION))

_function_servers: list[NatsServer] = []
_module_servers: list[NatsServer] = []


def test_core_fixture_yields_a_ready_read_only_server(core_nats: NatsServer) -> None:
    _module_servers.append(core_nats)
    assert core_nats.host == "127.0.0.1"
    assert core_nats.url == f"nats://127.0.0.1:{core_nats.port}"
    assert core_nats.resolved_version == _INTEGRATION_VERSION
    assert not core_nats.jetstream_enabled
    assert not hasattr(core_nats, "process")
    with pytest.raises(AttributeError):
        setattr(core_nats, "port", 4222)  # noqa: B010 - assignment is intentionally rejected at runtime.

    with socket.create_connection((core_nats.host, core_nats.port), timeout=1) as connection:
        assert connection.recv(65536).startswith(b"INFO ")
        connection.sendall(b"PING\r\n")
        assert b"PONG\r\n" in connection.recv(65536)


@pytest.mark.parametrize("invocation", [1, 2])
def test_function_scope_creates_a_server_for_each_invocation(function_nats: NatsServer, invocation: int) -> None:
    del invocation
    assert all(server is not function_nats for server in _function_servers)
    _function_servers.append(function_nats)


def test_separate_fixture_declarations_use_distinct_ports(
    core_nats: NatsServer,
    jetstream_nats: NatsServer,
) -> None:
    assert _module_servers == [core_nats]
    assert core_nats.port != jetstream_nats.port


def test_jetstream_fixture_supports_memory_and_file_streams(jetstream_nats: NatsServer) -> None:
    async def verify() -> None:
        client = await nats.connect(jetstream_nats.url)
        try:
            manager = client.jetstream()
            for name, storage in (("MEMORY", StorageType.MEMORY), ("FILE", StorageType.FILE)):
                await manager.add_stream(StreamConfig(name=name, subjects=[name.lower()], storage=storage))
                acknowledgement = await manager.publish(name.lower(), b"payload")
                assert acknowledgement.stream == name
                message = await manager.get_msg(name, seq=acknowledgement.seq)
                assert message.data == b"payload"
        finally:
            await client.close()

    assert jetstream_nats.jetstream_enabled
    asyncio.run(verify())
