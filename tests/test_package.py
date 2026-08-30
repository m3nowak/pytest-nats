from dataclasses import FrozenInstanceError
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, cast

from pytest import mark, raises

from pytest_nats import ExecutableErrorCategory, GitHub, Local, Mise, NatsExecutableError, Provision


def test_source_defaults_are_normalized() -> None:
    assert Local() == Local("nats-server")
    assert Provision() == Provision("latest")
    assert Mise() == Mise("latest")
    assert GitHub() == GitHub("latest")


def test_cache_configuration_is_keyword_only() -> None:
    assert signature(Provision).parameters["cache_dir"].kind is Parameter.KEYWORD_ONLY
    assert signature(GitHub).parameters["cache_dir"].kind is Parameter.KEYWORD_ONLY
    assert "cache_dir" not in signature(Mise).parameters


@mark.parametrize("source", [Local(), Provision(), Mise(), GitHub()])
def test_sources_are_immutable_and_slotted(source: object) -> None:
    assert not hasattr(source, "__dict__")
    with raises((FrozenInstanceError, AttributeError, TypeError)):
        setattr(source, "version", "2.12.15")  # noqa: B010 - assignment must fail at runtime.


@mark.parametrize("value", ["", ".", Path()])
def test_local_rejects_empty_and_current_directory_values(value: str | Path) -> None:
    with raises(NatsExecutableError) as raised:
        Local(value)

    assert raised.value.category is ExecutableErrorCategory.CONFIGURATION


@mark.parametrize("selector", ["Latest", "v2.12.15", "2.12.15-rc1", "2.1", "2.1.9", "3", 2])
@mark.parametrize("source_type", [Provision, Mise, GitHub])
def test_provisioning_sources_reject_invalid_selectors(source_type: type[object], selector: object) -> None:
    with raises(NatsExecutableError) as raised:
        cast(Any, source_type)(selector)

    assert raised.value.category is ExecutableErrorCategory.CONFIGURATION
