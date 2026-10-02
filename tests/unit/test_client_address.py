"""The client address behind the login limit (TRZ-40): only the header the edge proxy sets
counts. On Railway that is X-Real-IP; X-Forwarded-For reaches the service as the client wrote it
(31 requests with a spoofed one all passed on the public URL), so it is never read."""

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from app.api.deps import get_client_address
from app.core.config import Settings

SOCKET = "10.0.0.9"


def _address(
    header: str,
    sent: dict[str, str] | None = None,
    client: tuple[str, int] | None = (SOCKET, 5000),
) -> str:
    headers = [(k.encode(), v.encode()) for k, v in (sent or {}).items()]
    request = Request({"type": "http", "headers": headers, "client": client})
    settings = Settings(database_url="postgresql+psycopg://u:p@h/d", client_ip_header=header)
    runtime: Any = SimpleNamespace(settings=settings)
    return get_client_address(request, runtime)


def test_without_a_configured_header_the_socket_address_is_used() -> None:
    sent = {"x-real-ip": "198.51.100.7", "x-forwarded-for": "203.0.113.1"}
    assert _address("", sent) == SOCKET


def test_with_a_configured_header_its_value_is_the_address() -> None:
    assert _address("x-real-ip", {"x-real-ip": " 198.51.100.7 "}) == "198.51.100.7"


@pytest.mark.parametrize("name", ["X-Real-IP", "x-real-ip"])
def test_the_header_name_is_case_insensitive(name: str) -> None:
    assert _address(name, {"x-real-ip": "198.51.100.7"}) == "198.51.100.7"


def test_x_forwarded_for_is_never_read() -> None:
    sent = {"x-real-ip": "198.51.100.7", "x-forwarded-for": "203.0.113.1, 192.0.2.4"}
    assert _address("x-real-ip", sent) == "198.51.100.7"


@pytest.mark.parametrize("sent", [None, {"x-real-ip": ""}, {"x-real-ip": "  "}])
def test_a_missing_configured_header_is_one_shared_unknown_address(
    sent: dict[str, str] | None,
) -> None:
    # Never the socket: behind the edge it is an internal address that changes per connection,
    # which would give each request a fresh count.
    assert _address("x-real-ip", sent) == "unknown"


def test_a_request_without_a_socket_address_is_unknown() -> None:
    assert _address("", None, client=None) == "unknown"


def test_the_default_limit_is_30_requests_in_15_minutes_by_socket_address() -> None:
    s = Settings(database_url="postgresql+psycopg://u:p@h/d", _env_file=None)  # type: ignore[call-arg]
    assert (s.ip_request_limit, s.ip_request_window_minutes, s.client_ip_header) == (30, 15, "")
