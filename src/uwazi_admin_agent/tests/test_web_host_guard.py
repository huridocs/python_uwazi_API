"""Offline tests for the web bind-address guard (pure)."""

import pytest

from uwazi_admin_agent.drivers.web.host_guard import assert_safe_bind, is_loopback_host


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert is_loopback_host(host) is True
    assert_safe_bind(host, "http://anything", None)  # loopback needs no HTTPS/secret


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "example.com", ""])
def test_non_loopback_hosts_are_refused(host: str) -> None:
    assert is_loopback_host(host) is False


def test_non_loopback_requires_https_public_url() -> None:
    with pytest.raises(SystemExit):
        assert_safe_bind("0.0.0.0", "http://admin.example.com", "secret")


def test_non_loopback_requires_a_stable_storage_secret() -> None:
    with pytest.raises(SystemExit):
        assert_safe_bind("0.0.0.0", "https://admin.example.com", None)


def test_non_loopback_with_https_and_secret_is_accepted() -> None:
    assert_safe_bind("0.0.0.0", "https://admin.example.com", "secret")
