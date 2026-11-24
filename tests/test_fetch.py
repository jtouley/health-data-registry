# SPDX-License-Identifier: Apache-2.0
# Adapted from md_data_explorer tests/storage/test_url_fetch.py and tests/fixtures/https_server.py (MIT).
"""HTTPS fetcher: allowlist, SSRF address checks, redirects, rebinding, size, time, proxy tunnel.

Every request goes to a local HTTPS server on 127.0.0.1 with a throwaway certificate; fake DNS
names resolve to public-looking labels that the fixture routes to that server. Nothing reaches the
internet except the test marked ``network``.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import ipaddress
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from health_data_registry import fetch as fetch_mod
from health_data_registry.fetch import (
    MAX_REDIRECTS,
    USER_AGENT,
    FetchRefused,
    fetch,
    is_public_address,
    public_addresses,
    validate_url,
)

# Made-up names -> the addresses the fake DNS gives. 93.184.215.0/24 is public space, used only
# as a label: connections to it are routed to the local server.
FAKE_DNS: dict[str, list[str]] = {
    "data.example.test": ["93.184.215.14"],
    "mirror.example.test": ["93.184.215.15"],
    "internal.example.test": ["10.0.0.5"],
    "metadata.example.test": ["169.254.169.254"],
    "mixed.example.test": ["93.184.215.14", "127.0.0.1"],
    "cgnat.example.test": ["100.64.1.1"],
    "mapped.example.test": ["::ffff:127.0.0.1"],
    "nocert.example.test": ["93.184.215.20"],  # public, but not named in the certificate
}
CERT_NAMES = ("data.example.test", "mirror.example.test", "other.example.test")
ALLOWED = frozenset(FAKE_DNS) | {"other.example.test"}
CSV = ("state,year,rate\n" + "".join(f"S{i:02d},{2000 + i % 24},{i * 1.5}\n" for i in range(200))).encode()


# ---------------------------------------------------------------------------
# Local HTTPS server and CONNECT proxy
# ---------------------------------------------------------------------------


@dataclass
class Canned:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    delay_before_body_s: float = 0.0
    trickle_chunks: int = 0
    trickle_pause_s: float = 0.0
    chunked: bool = False


def _split(body: bytes, pieces: int) -> list[bytes]:
    if pieces <= 1 or not body:
        return [body]
    size = max(1, -(-len(body) // pieces))
    return [body[i : i + size] for i in range(0, len(body), size)]


class LocalHttpsSource:
    def __init__(self, cert: Path, key: Path) -> None:
        self.cert = cert
        self.routes: dict[str, Canned] = {}
        self.requests: list[dict[str, str]] = []
        self.connections: list[str] = []
        source = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                source.requests.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
                canned = source.routes.get(self.path, Canned(status=404, body=b"no such file"))
                self.send_response(canned.status)
                for name, value in canned.headers.items():
                    self.send_header(name, value)
                if canned.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                elif "Content-Length" not in canned.headers:
                    self.send_header("Content-Length", str(len(canned.body)))
                self.end_headers()
                if canned.delay_before_body_s:
                    time.sleep(canned.delay_before_body_s)
                try:
                    for piece in _split(canned.body, canned.trickle_chunks):
                        if canned.chunked:
                            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                        else:
                            self.wfile.write(piece)
                        self.wfile.flush()
                        if canned.trickle_pause_s:
                            time.sleep(canned.trickle_pause_s)
                    if canned.chunked:
                        self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                    return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = (
            ssl.TLSVersion.TLSv1_2
        )  # as the client requires; no legacy TLS even in tests
        context.load_cert_chain(cert, key)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def client_context(self, *, proxied: bool = False) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=str(self.cert))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def url(self, path: str, host: str = "data.example.test") -> str:
        return f"https://{host}{path}"


class ConnectProxy:
    """A minimal HTTP CONNECT proxy that tunnels every allowed CONNECT to the local HTTPS server."""

    def __init__(self, upstream_port: int, *, answer: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n"):
        self.upstream_port = upstream_port
        self.answer = answer
        self.connects: list[str] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        with client:
            head = b""
            while not head.endswith(b"\r\n\r\n"):
                byte = client.recv(1)
                if not byte:
                    return
                head += byte
            self.connects.append(head.split(b"\r\n", 1)[0].decode())
            client.sendall(self.answer)
            if not self.answer.startswith(b"HTTP/1.1 200"):
                return
            with socket.create_connection(("127.0.0.1", self.upstream_port)) as upstream:
                self._pipe(client, upstream)

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        def copy(src: socket.socket, dst: socket.socket) -> None:
            try:
                while data := src.recv(65536):
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    dst.shutdown(socket.SHUT_WR)

        t = threading.Thread(target=copy, args=(b, a), daemon=True)
        t.start()
        copy(a, b)
        t.join(5)

    def close(self) -> None:
        self._stop.set()
        self._listener.close()


@pytest.fixture(scope="session")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:  # pragma: no cover - CI images ship openssl
        pytest.skip("openssl is needed to make the test certificate")
    directory = tmp_path_factory.mktemp("https-cert")
    cert, key = directory / "cert.pem", directory / "key.pem"
    san = ",".join(f"DNS:{name}" for name in CERT_NAMES)
    subprocess.run(  # noqa: S603 - fixed arguments, test-only
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=data.example.test",
         "-addext", f"subjectAltName={san}", "-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


@pytest.fixture
def source(certificate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalHttpsSource]:
    """A running local HTTPS server; the fetcher's DNS, TCP connect and trust store point at it."""
    server = LocalHttpsSource(*certificate)
    public = {a for addrs in FAKE_DNS.values() for a in addrs if ipaddress.ip_address(a).is_global}
    real_getaddrinfo = socket.getaddrinfo
    real_open_tcp = fetch_mod._open_tcp

    def fake_getaddrinfo(host: str, port: int, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        if host in FAKE_DNS:
            return [
                (
                    socket.AF_INET6 if ":" in a else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (a, port),
                )
                for a in FAKE_DNS[host]
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)  # type: ignore[arg-type]

    def fake_open_tcp(address: str, port: int, timeout: float) -> socket.socket:
        if address == "127.0.0.1" and port != 443:  # the test proxy
            return real_open_tcp(address, port, timeout)
        server.connections.append(address)
        assert address in public, f"fetcher connected to non-public address {address}"
        return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

    monkeypatch.delenv("HDR_HTTPS_PROXY", raising=False)
    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(fetch_mod, "_open_tcp", fake_open_tcp)
    monkeypatch.setattr(fetch_mod, "_tls_context", server.client_context)
    server.start()
    yield server
    server.stop()


def _get(url: str, **kwargs: object) -> fetch_mod.FetchResult:
    return fetch(url, allowed_hosts=ALLOWED, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Link checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("", "invalid_url"),
        ("   ", "invalid_url"),
        ("http://data.example.test/a.csv", "insecure_scheme"),
        ("ftp://data.example.test/a.csv", "insecure_scheme"),
        ("file:///etc/passwd", "insecure_scheme"),
        ("javascript:alert(1)", "insecure_scheme"),
        ("https://", "invalid_url"),
        ("https://user:pw@data.example.test/a.csv", "invalid_url"),
        ("https://token@data.example.test/a.csv", "invalid_url"),
        ("https://data.example.test:8443/a.csv", "invalid_url"),
        ("https://data.example.test:99999/a.csv", "invalid_url"),
        ("https://[::1/a.csv", "invalid_url"),
        ("https://data.example.test/a b.csv", "invalid_url"),
        ("https://data.example.test/a\r\nX-Injected: 1", "invalid_url"),
        ("https://data.example.test/" + "a" * 5000, "invalid_url"),
        ("https://evil.example.test/a.csv", "host_not_allowed"),
        ("https://data.example.test.evil.test/a.csv", "host_not_allowed"),
        ("https://sub.data.example.test/a.csv", "host_not_allowed"),
        ("https://data.example.test./a.csv", "host_not_allowed"),
        ("https://127.0.0.1/a.csv", "host_not_allowed"),
        ("https://[::1]/a.csv", "host_not_allowed"),
        ("https://169.254.169.254/latest/meta-data/", "host_not_allowed"),
    ],
)
def test_validateUrl_unsafeOrNotAllowed_refusedWithCode(url: str, code: str) -> None:
    with pytest.raises(FetchRefused) as caught:
        validate_url(url, ALLOWED)
    assert caught.value.code == code
    assert caught.value.message


@pytest.mark.parametrize(
    "url",
    [
        "https://data.example.test/a.csv",
        "https://data.example.test:443/a?x=1",
        "  https://DATA.example.test/ ",
    ],
)
def test_validateUrl_plainHttpsAllowedHost_accepted(url: str) -> None:
    assert validate_url(url, ALLOWED).hostname == "data.example.test"


def test_validateUrl_allowlistCaseInsensitive() -> None:
    assert (
        validate_url("https://Data.Example.Test/", frozenset({"DATA.EXAMPLE.TEST"})).hostname
        == "data.example.test"
    )


# ---------------------------------------------------------------------------
# Address checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.215.14", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("0.0.0.0", False),  # noqa: S104 - an address under test, not a bind
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
        ("192.0.2.1", False),
        ("198.18.0.1", False),
        ("::1", False),
        ("::", False),
        ("::7f00:1", False),  # IPv4-compatible loopback
        ("fe80::1", False),
        ("fc00::1", False),
        ("ff02::1", False),
        ("2001:db8::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:93.184.215.14", True),
        ("64:ff9b::7f00:1", False),
        ("64:ff9b::a00:1", False),
        ("64:ff9b::5db8:d70e", True),
        ("64:ff9b:1::a00:1", False),
        ("2002:7f00:1::1", False),
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False),
    ],
)
def test_isPublicAddress_rangeTable(address: str, public: bool) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is public


_BLOCKED_V4 = [
    ipaddress.IPv4Network(n)
    for n in ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
              "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
              "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4")
]  # fmt: skip
_BLOCKED_V6 = [
    ipaddress.IPv6Network(n)
    for n in (
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
        "2002::/16",
        "2001::/32",
    )
]


@given(st.ip_addresses(v=4))
def test_isPublicAddress_ipv4Property_neverInBlockedRanges(ip: ipaddress.IPv4Address) -> None:
    public = is_public_address(ip)
    if any(ip in net for net in _BLOCKED_V4):
        assert not public
    if public:
        assert ip.is_global and not ip.is_private


@given(st.ip_addresses(v=4))
def test_isPublicAddress_embeddedForms_followTheIpv4Address(ip: ipaddress.IPv4Address) -> None:
    mapped = ipaddress.IPv6Address(f"::ffff:{ip}")
    nat64 = ipaddress.IPv6Address(int(ipaddress.IPv6Address("64:ff9b::")) | int(ip))
    assert is_public_address(mapped) is is_public_address(ip)
    assert is_public_address(nat64) is is_public_address(ip)
    assert not is_public_address(ipaddress.IPv6Address(int(ip)))  # IPv4-compatible


@given(st.ip_addresses(v=6))
def test_isPublicAddress_ipv6Property_neverInBlockedRanges(ip: ipaddress.IPv6Address) -> None:
    public = is_public_address(ip)
    if any(ip in net for net in _BLOCKED_V6):
        assert not public
    if public:
        assert ip.ipv4_mapped is None or is_public_address(ip.ipv4_mapped)


@pytest.mark.parametrize(
    "host",
    [
        "internal.example.test",
        "metadata.example.test",
        "mixed.example.test",
        "cgnat.example.test",
        "mapped.example.test",
    ],
)
def test_fetch_nonPublicHost_blockedBeforeConnecting(source: LocalHttpsSource, host: str) -> None:
    with pytest.raises(FetchRefused) as caught:
        _get(f"https://{host}/a.csv")
    assert caught.value.code == "blocked_address"
    assert source.connections == []


def test_publicAddresses_unparseableAnswer_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_mod, "_resolve", lambda host: ["not-an-ip"])
    with pytest.raises(FetchRefused) as caught:
        public_addresses("data.example.test")
    assert caught.value.code == "blocked_address"


def test_publicAddresses_emptyAnswer_dnsFailed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_mod, "_resolve", lambda host: [])
    with pytest.raises(FetchRefused) as caught:
        public_addresses("data.example.test")
    assert caught.value.code == "dns_failed"


def test_fetch_unknownHost_dnsFailed(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_such_host(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", no_such_host)
    with pytest.raises(FetchRefused) as caught:
        _get("https://data.example.test/a.csv")
    assert caught.value.code == "dns_failed"


def test_fetch_dnsRebinding_connectsOnlyToCheckedAddress(
    source: LocalHttpsSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    source.routes["/rates.csv"] = Canned(headers={"Content-Type": "text/csv"}, body=CSV)
    answers = iter([["93.184.215.14"], ["127.0.0.1"], ["127.0.0.1"]])
    calls: list[str] = []

    real_getaddrinfo = socket.getaddrinfo

    def rebinding(host: str, port: int, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        if host == "127.0.0.1":  # the fixture's own connection to the local server
            return real_getaddrinfo(host, port, *args, **kwargs)  # type: ignore[arg-type]
        calls.append(host)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (a, port)) for a in next(answers)
        ]

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", rebinding)
    result = _get("https://data.example.test/rates.csv")
    assert calls == ["data.example.test"]
    assert source.connections == ["93.184.215.14"]
    assert result.content == CSV


# ---------------------------------------------------------------------------
# Successful fetches
# ---------------------------------------------------------------------------


def test_fetch_ok_returnsBytesAndProvenance(source: LocalHttpsSource) -> None:
    path = "/api/views/abcd/rows.csv?accessType=DOWNLOAD"
    source.routes[path] = Canned(headers={"Content-Type": "text/csv; charset=utf-8"}, body=CSV)
    url = source.url(path)
    result = _get(url)
    assert result.content == CSV
    assert result.url == result.final_url == url
    assert result.status == 200
    assert result.sha256 == hashlib.sha256(CSV).hexdigest()
    assert result.content_type == "text/csv; charset=utf-8"
    assert result.retrieved_at.tzinfo is UTC


def test_fetch_requestHeaders_userAgentAndNothingElsePrivate(source: LocalHttpsSource) -> None:
    source.routes["/a.csv"] = Canned(body=CSV)
    _get(source.url("/a.csv"), headers={"X-App-Token": "abc"})
    request = source.requests[0]
    assert request["host"] == "data.example.test"
    assert request["user-agent"] == USER_AGENT
    assert "@" not in USER_AGENT and USER_AGENT.startswith("health-data-registry/")
    assert request["x-app-token"] == "abc"
    assert not {"cookie", "authorization", "referer", "proxy-authorization"} & set(request)


@pytest.mark.parametrize(
    "headers",
    [{"Host": "evil"}, {"User-Agent": "x"}, {"Bad Name": "x"}, {"X-A": "a\r\nX-B: b"}, {"Cookie": "a=b"}],
)
def test_fetch_badCallerHeader_refused(headers: dict[str, str]) -> None:
    with pytest.raises(FetchRefused) as caught:
        _get("https://data.example.test/a.csv", headers=headers)
    assert caught.value.code == "bad_header"


@pytest.mark.parametrize(("kwargs"), [{"max_bytes": 0}, {"timeout": 0}, {"timeout": float("nan")}])
def test_fetch_nonPositiveLimits_valueError(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="positive"):
        _get("https://data.example.test/a.csv", **kwargs)


def test_fetch_gzip_inflatedAndHashedAsInflated(source: LocalHttpsSource) -> None:
    source.routes["/a.csv"] = Canned(headers={"Content-Encoding": "gzip"}, body=gzip.compress(CSV))
    result = _get(source.url("/a.csv"))
    assert result.content == CSV
    assert result.sha256 == hashlib.sha256(CSV).hexdigest()


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        ("gzip", gzip.compress(CSV[:1000]) + gzip.compress(CSV[1000:])),
        ("deflate", zlib.compress(CSV)),
        ("deflate", (lambda c: c.compress(CSV) + c.flush())(zlib.compressobj(wbits=-zlib.MAX_WBITS))),
        ("identity", CSV),
    ],
    ids=["gzip-multi-member", "deflate-zlib", "deflate-raw", "identity"],
)
def test_fetch_compressedVariants_inflatedWhole(source: LocalHttpsSource, encoding: str, body: bytes) -> None:
    source.routes["/a.csv"] = Canned(headers={"Content-Encoding": encoding}, body=body, trickle_chunks=3)
    assert _get(source.url("/a.csv")).content == CSV


@pytest.mark.parametrize(
    ("encoding", "body", "code"),
    [
        ("gzip", b"\x1f\x8b\x08\x00garbage" * 20, "damaged"),
        ("gzip", gzip.compress(CSV)[:-30], "damaged"),
        ("deflate", b"\xff\xfe not deflate at all", "damaged"),
        ("deflate", zlib.compress(CSV) + b"trailing", "damaged"),
        ("br", b"brotli bytes", "bad_encoding"),
    ],
    ids=["gzip-garbage", "gzip-truncated", "deflate-garbage", "deflate-trailing", "brotli"],
)
def test_fetch_badEncoding_refused(source: LocalHttpsSource, encoding: str, body: bytes, code: str) -> None:
    source.routes["/a.csv"] = Canned(headers={"Content-Encoding": encoding}, body=body)
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/a.csv"))
    assert caught.value.code == code


def test_fetch_chunkedTransfer_readFully(source: LocalHttpsSource) -> None:
    source.routes["/a.csv"] = Canned(body=CSV, chunked=True, trickle_chunks=4)
    assert _get(source.url("/a.csv")).content == CSV


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


def test_fetch_redirectToAllowedHost_followedAndRecorded(source: LocalHttpsSource) -> None:
    source.routes["/latest"] = Canned(
        status=302, headers={"Location": "https://mirror.example.test/files/2026.csv"}
    )
    source.routes["/files/2026.csv"] = Canned(body=CSV)
    result = _get(source.url("/latest"), headers={"X-App-Token": "secret"})
    assert result.url == "https://data.example.test/latest"
    assert result.final_url == "https://mirror.example.test/files/2026.csv"
    assert source.connections == ["93.184.215.14", "93.184.215.15"]
    assert source.requests[0]["x-app-token"] == "secret"
    assert "x-app-token" not in source.requests[1]  # caller headers stay with the original host


def test_fetch_relativeRedirect_sameHostKeepsHeaders(source: LocalHttpsSource) -> None:
    source.routes["/old"] = Canned(status=301, headers={"Location": "/new"})
    source.routes["/new"] = Canned(body=CSV)
    result = _get(source.url("/old"), headers={"X-App-Token": "t"})
    assert result.final_url == "https://data.example.test/new"
    assert source.requests[1]["x-app-token"] == "t"


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("https://internal.example.test/a.csv", "blocked_address"),
        ("https://evil.example.test/a.csv", "host_not_allowed"),
        ("https://127.0.0.1/a.csv", "host_not_allowed"),
        ("https://169.254.169.254/latest/meta-data/", "host_not_allowed"),
        ("http://data.example.test/a.csv", "insecure_scheme"),
        ("file:///etc/passwd", "insecure_scheme"),
        ("https://data.example.test:8080/a.csv", "invalid_url"),
    ],
)
def test_fetch_redirectToUnsafeTarget_refused(source: LocalHttpsSource, location: str, code: str) -> None:
    source.routes["/go"] = Canned(status=301, headers={"Location": location})
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/go"))
    assert caught.value.code == code
    assert source.connections == ["93.184.215.14"]


def test_fetch_redirectLoop_stoppedAfterLimit(source: LocalHttpsSource) -> None:
    source.routes["/loop"] = Canned(status=307, headers={"Location": "/loop"})
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/loop"))
    assert caught.value.code == "too_many_redirects"
    assert len(source.requests) == MAX_REDIRECTS + 1


def test_fetch_redirectWithoutLocation_httpError(source: LocalHttpsSource) -> None:
    source.routes["/go"] = Canned(status=302)
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/go"))
    assert caught.value.code == "http_error"
    assert caught.value.status == 302


# ---------------------------------------------------------------------------
# Status, TLS, size and time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [204, 401, 403, 404, 410, 429, 500, 503])
def test_fetch_errorStatus_httpError(source: LocalHttpsSource, status: int) -> None:
    source.routes["/a.csv"] = Canned(status=status, body=b"" if status == 204 else b"nope")
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/a.csv"))
    assert caught.value.code == "http_error"
    assert caught.value.status == status
    assert str(status) in caught.value.message


def test_fetch_untrustedCertificate_tlsError(
    source: LocalHttpsSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_mod, "_tls_context", lambda *, proxied: ssl.create_default_context())
    source.routes["/a.csv"] = Canned(body=CSV)
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/a.csv"))
    assert caught.value.code == "tls_error"
    assert source.requests == []


def test_fetch_certificateForAnotherName_tlsError(source: LocalHttpsSource) -> None:
    source.routes["/a.csv"] = Canned(body=CSV)
    with pytest.raises(FetchRefused) as caught:
        _get("https://nocert.example.test/a.csv")
    assert caught.value.code == "tls_error"
    assert source.requests == []


def test_fetch_connectionRefused_connectFailed(
    source: LocalHttpsSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(address: str, port: int, timeout: float) -> socket.socket:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(fetch_mod, "_open_tcp", refuse)
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/a.csv"))
    assert caught.value.code == "connect_failed"


@pytest.mark.parametrize(
    "canned",
    [
        Canned(body=CSV),
        Canned(body=CSV, chunked=True, trickle_chunks=8),
        Canned(headers={"Content-Encoding": "gzip"}, body=gzip.compress(b"0" * 10_000_000)),
        Canned(headers={"Content-Encoding": "deflate"}, body=zlib.compress(b"0" * 10_000_000)),
    ],
    ids=["content-length", "chunked", "gzip-bomb", "deflate-bomb"],
)
def test_fetch_overSizeCap_tooLarge(source: LocalHttpsSource, canned: Canned) -> None:
    source.routes["/big.csv"] = canned
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/big.csv"), max_bytes=1000)
    assert caught.value.code == "too_large"


def test_fetch_compressedOverWireCap_tooLarge(source: LocalHttpsSource) -> None:
    body = gzip.compress(os.urandom(5000))  # incompressible: the wire size itself is over the cap
    source.routes["/big.csv"] = Canned(headers={"Content-Encoding": "gzip"}, body=body, chunked=True)
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/big.csv"), max_bytes=1000)
    assert caught.value.code == "too_large"


def test_fetch_exactlyAtSizeCap_accepted(source: LocalHttpsSource) -> None:
    source.routes["/a.csv"] = Canned(body=CSV)
    assert len(_get(source.url("/a.csv"), max_bytes=len(CSV)).content) == len(CSV)


def test_fetch_serverStallsBeforeBody_timeout(
    source: LocalHttpsSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_mod, "READ_TIMEOUT_S", 0.3)
    source.routes["/slow.csv"] = Canned(body=CSV, delay_before_body_s=2.0)
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/slow.csv"))
    assert caught.value.code == "timeout"


def test_fetch_tricklingServer_stoppedByOverallDeadline(source: LocalHttpsSource) -> None:
    source.routes["/drip.csv"] = Canned(body=CSV, trickle_chunks=len(CSV), trickle_pause_s=0.1)
    started = time.monotonic()
    with pytest.raises(FetchRefused) as caught:
        _get(source.url("/drip.csv"), timeout=0.8)
    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 5


def test_deadline_passed_raisesTimeout() -> None:
    deadline = fetch_mod._Deadline(0.0)
    with pytest.raises(FetchRefused) as caught:
        deadline.remaining()
    assert caught.value.code == "timeout"


# ---------------------------------------------------------------------------
# Egress proxy (HDR_HTTPS_PROXY)
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy(source: LocalHttpsSource, monkeypatch: pytest.MonkeyPatch) -> Iterator[ConnectProxy]:
    server = ConnectProxy(source.port)
    monkeypatch.setenv("HDR_HTTPS_PROXY", f"http://127.0.0.1:{server.port}")
    yield server
    server.close()


def test_fetch_viaProxy_tunnelsByHostNameWithTls(source: LocalHttpsSource, proxy: ConnectProxy) -> None:
    source.routes["/a.csv"] = Canned(headers={"Content-Encoding": "gzip"}, body=gzip.compress(CSV))
    result = _get(source.url("/a.csv"))
    assert result.content == CSV
    assert proxy.connects == ["CONNECT data.example.test:443 HTTP/1.1"]
    assert source.connections == []  # never a direct connection
    assert source.requests[0]["host"] == "data.example.test"


def test_fetch_viaProxy_addressCheckStillLocal(source: LocalHttpsSource, proxy: ConnectProxy) -> None:
    with pytest.raises(FetchRefused) as caught:
        _get("https://internal.example.test/a.csv")
    assert caught.value.code == "blocked_address"
    assert proxy.connects == []


def test_fetch_viaProxy_redirectRecheckedAndTunnelledAgain(
    source: LocalHttpsSource, proxy: ConnectProxy
) -> None:
    source.routes["/latest"] = Canned(status=302, headers={"Location": "https://mirror.example.test/b.csv"})
    source.routes["/b.csv"] = Canned(body=CSV)
    assert _get(source.url("/latest")).content == CSV
    assert proxy.connects == [
        "CONNECT data.example.test:443 HTTP/1.1",
        "CONNECT mirror.example.test:443 HTTP/1.1",
    ]


@pytest.mark.parametrize(
    "answer",
    [b"HTTP/1.1 403 Forbidden\r\n\r\n", b"garbage\r\n\r\n", b"HTTP/1.1 200 OK\r\n" + b"X: y\r\n" * 3000, b""],
)
def test_fetch_proxyRefusesOrBreaks_proxyError(
    source: LocalHttpsSource, monkeypatch: pytest.MonkeyPatch, answer: bytes
) -> None:
    server = ConnectProxy(source.port, answer=answer)
    monkeypatch.setenv("HDR_HTTPS_PROXY", f"http://127.0.0.1:{server.port}")
    try:
        with pytest.raises(FetchRefused) as caught:
            _get(source.url("/a.csv"))
    finally:
        server.close()
    assert caught.value.code == "proxy_error"


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:3128",
        "socks5://127.0.0.1:1080",
        "http://user:pw@127.0.0.1:3128",
        "http://",
        "http://h:x",
    ],
)
def test_fetch_badProxySetting_proxyConfig(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("HDR_HTTPS_PROXY", value)
    with pytest.raises(FetchRefused) as caught:
        _get("https://data.example.test/a.csv")
    assert caught.value.code == "proxy_config"


def test_tlsContext_proxied_addsCaBundleFromEnvironment(
    certificate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate[0]))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/bundle.pem")
    context = fetch_mod._tls_context(proxied=True)
    subjects = [dict(x[0] for x in c["subject"]).get("commonName") for c in context.get_ca_certs()]
    assert "data.example.test" in subjects
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_tlsContext_direct_verifies() -> None:
    context = fetch_mod._tls_context(proxied=False)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_fetch_live_cdcSocrataMetadata(request: pytest.FixtureRequest) -> None:
    # Runs only when asked for (pytest -m network); behind an egress proxy set HDR_HTTPS_PROXY.
    if "network" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("live publisher test: run with -m network")
    result = fetch("https://data.cdc.gov/api/views/x9gk-5huc.json", allowed_hosts=frozenset({"data.cdc.gov"}))
    assert result.status == 200
    assert b'"id"' in result.content
