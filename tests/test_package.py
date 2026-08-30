def test_package_can_be_imported() -> None:
    import pytest_nats

    assert pytest_nats.__doc__
