"""The smoke journey runs offline, and this enforces it: any socket connection off this machine fails the test.

HDR_HUB=off and HDR_FETCH_AT_RUN_TIME=0 (set by the autouse fixture in tests/conftest.py) are switches a code
path could ignore. Here a stray fetch to a publisher, the hub or anywhere else raises instead of quietly reading
live data, so a smoke result can only come from the synthetic lake.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest

LOCAL_NAMES = {"localhost", ""}


class NetworkBlocked(AssertionError):
    """A smoke test tried to reach the network."""


def _is_local(address: Any) -> bool:
    if not isinstance(address, tuple):  # AF_UNIX path: never leaves the machine
        return True
    raw = address[0]
    host = raw.decode() if isinstance(raw, bytes) else str(raw)
    if host in LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False  # a host name: resolving it would already be network use


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(address: Any) -> None:
        if not _is_local(address):
            raise NetworkBlocked(f"the smoke journey is offline; a test tried to connect to {address!r}")

    def connect(self: socket.socket, address: Any) -> None:
        guard(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        guard(address)
        return real_connect_ex(self, address)

    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        guard((host if host is not None else "", 0))  # a DNS lookup for a remote host is network use too
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
