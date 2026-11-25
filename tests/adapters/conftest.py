"""Fixtures for the adapter-type and extension tests."""

from __future__ import annotations

import socket
from typing import Any

import pytest


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network at all: an extension that tried to fetch anything would fail its test."""

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("sockets are disabled in extension tests")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
