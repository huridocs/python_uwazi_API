"""Unit tests for the maintenance-error detection helper in ``request_retry``.

Pure, offline, no mocks: ``is_maintenance_error`` is a deterministic predicate
over a status code and an optional body string.
"""

from uwazi_api.adapters.request_retry import is_maintenance_error


def test_gateway_status_codes_are_maintenance() -> None:
    for code in (502, 503, 504):
        assert is_maintenance_error(code) is True


def test_maintenance_body_confirms_other_5xx() -> None:
    assert is_maintenance_error(500, "<html>We're down for maintenance</html>") is True
    assert is_maintenance_error(500, "temporarily unavailable") is True


def test_plain_5xx_without_marker_is_not_maintenance() -> None:
    assert is_maintenance_error(500, "internal server error") is False
    assert is_maintenance_error(500) is False


def test_non_5xx_is_not_maintenance() -> None:
    assert is_maintenance_error(200) is False
    assert is_maintenance_error(404) is False
    assert is_maintenance_error(429) is False


def test_none_status_is_not_maintenance() -> None:
    assert is_maintenance_error(None) is False
    assert is_maintenance_error(None, "maintenance") is False
