"""The client address behind the login limit (TRZ-40): only entries written by trusted proxies
count, so a client cannot pick its own address with X-Forwarded-For."""

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from app.api.deps import get_client_address
from app.core.config import Settings

SOCKET = "10.0.0.9"


def _address(
    hops: int, forwarded: str | None, client: tuple[str, int] | None = (SOCKET, 5000)
) -> str:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    request = Request({"type": "http", "headers": headers, "client": client})
    settings = Settings(database_url="postgresql+psycopg://u:p@h/d", trusted_proxy_hops=hops)
    runtime: Any = SimpleNamespace(settings=settings)
    return get_client_address(request, runtime)


def test_without_trusted_proxies_the_header_is_ignored() -> None:
    assert _address(0, "198.51.100.7") == SOCKET


def test_behind_one_proxy_the_address_is_the_entry_it_appended() -> None:
    assert _address(1, "198.51.100.7") == "198.51.100.7"


def test_an_entry_written_by_the_client_is_not_used() -> None:
    assert _address(1, "203.0.113.1, 198.51.100.7") == "198.51.100.7"


def test_two_proxies_take_the_second_entry_from_the_right() -> None:
    assert _address(2, "203.0.113.1, 198.51.100.7, 10.1.1.1") == "198.51.100.7"


@pytest.mark.parametrize("forwarded", [None, "", " , "])
def test_without_entries_the_socket_address_is_used(forwarded: str | None) -> None:
    assert _address(1, forwarded) == SOCKET


def test_fewer_entries_than_proxies_fall_back_to_the_socket() -> None:
    assert _address(2, "198.51.100.7") == SOCKET


def test_a_request_without_a_socket_address_is_unknown() -> None:
    assert _address(0, None, client=None) == "unknown"


def test_the_default_limit_is_30_requests_in_15_minutes() -> None:
    s = Settings(database_url="postgresql+psycopg://u:p@h/d", _env_file=None)  # type: ignore[call-arg]
    assert (s.ip_request_limit, s.ip_request_window_minutes, s.trusted_proxy_hops) == (30, 15, 0)
