# SPDX-License-Identifier: Apache-2.0
# Adapted from the author's earlier MIT-licensed work (see NOTICE).
"""Fetch one resource from a catalogued publisher over HTTPS, refusing anything else.

Every URL this package fetches comes from a reviewed catalog entry, but a catalog entry can be
wrong and a publisher can redirect anywhere, so the safe behaviour is the only behaviour:

- HTTPS on port 443 only, with certificate and host-name verification; no user:password in links.
- The host must be in ``allowed_hosts``: exact, case-insensitive match, no wildcards, no IP literals
  unless listed. Every redirect (at most ``MAX_REDIRECTS``) is checked again from the start.
- Every address the host resolves to must be a globally routable unicast address. Loopback,
  private networks, link-local (cloud metadata), carrier-grade NAT, multicast, reserved and
  documentation ranges, and IPv6 forms that embed an IPv4 address (IPv4-mapped, NAT64, 6to4,
  Teredo) are refused, so one bad answer in a mixed reply refuses the host.
- The TCP connection goes to an address that was checked (no second DNS lookup, so DNS rebinding
  cannot swap it); SNI, the certificate check and the Host header use the original host name.
- The body is streamed with a hard byte cap (Content-Length is checked first but not trusted);
  gzip and deflate are inflated with the same cap on the inflated size, so a small bomb cannot
  expand into gigabytes. Other content encodings are refused.
- Connect and per-read timeouts, plus one deadline for the whole fetch (redirects included),
  enforced by a watchdog that shuts the socket, so a server sending a byte every few seconds
  cannot hold the request open.
- No cookies, no Referer, no credentials beyond headers the caller passes; those are sent only to
  the original host and dropped on a redirect to another host.

**Egress proxy (``HDR_HTTPS_PROXY``).** By default the fetcher connects directly to the vetted
address: the production path (e.g. GitHub Actions). Some sandboxes only allow egress through an
HTTP CONNECT proxy; when ``HDR_HTTPS_PROXY`` is set (``http://host:port``), the fetcher still
resolves the host and refuses non-public answers locally, then opens a CONNECT tunnel to the proxy
naming the *host name* (``CONNECT data.cdc.gov:443``) and runs TLS end to end through it, with SNI
and verification against the original host. Trade-off: the proxy does its own DNS lookup, so the
local address check is advisory in this mode (the TCP target is no longer pinned to the checked
address); the operator-chosen proxy is trusted to enforce its own egress policy. A proxy that
re-terminates TLS needs its CA trusted: in proxy mode the file named by ``HDR_EXTRA_CA_FILE`` (and only
that one) is added to the default trust store. The generic ``HTTPS_PROXY`` and ``REQUESTS_CA_BUNDLE``
variables are deliberately ignored, so a stray environment variable set by another tool can neither route
production traffic nor widen the CAs trusted for a publisher (``SSL_CERT_FILE`` is read by OpenSSL itself
for every TLS context, in or out of proxy mode). Proxy mode logs one
warning per process saying that the address checks are advisory.

Failures raise :class:`FetchRefused` with a short machine ``code`` and a plain sentence.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import logging
import os
import re
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final
from urllib.parse import SplitResult, urljoin, urlsplit

from health_data_registry import __version__

logger = logging.getLogger(__name__)

MAX_URL_LENGTH: Final = 4096
MAX_REDIRECTS: Final = 5
HTTPS_PORT: Final = 443
CONNECT_TIMEOUT_S: Final = 15.0
READ_TIMEOUT_S: Final = 30.0
CHUNK_BYTES: Final = 64 * 1024
MAX_PROXY_REPLY_BYTES: Final = 16 * 1024
PROXY_ENV: Final = "HDR_HTTPS_PROXY"
USER_AGENT: Final = f"health-data-registry/{__version__} (+https://github.com/jtouley/health-data-registry)"

REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
# Statuses whose Retry-After header is read (RFC 9110 10.2.3): too many requests, service unavailable.
RETRY_AFTER_STATUSES: Final = frozenset({429, 503})
# Headers the fetcher owns: a caller cannot override them.
_RESERVED_HEADERS: Final = frozenset(
    {"host", "connection", "content-length", "transfer-encoding", "accept-encoding", "te", "upgrade",
     "user-agent", "proxy-authorization", "proxy-connection", "cookie", "referer", "trailer", "expect"}
)  # fmt: skip
_HEADER_NAME: Final = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

# Prefixes whose IPv6 addresses carry an IPv4 address that decides where packets go.
_NAT64_PREFIXES: Final = (ipaddress.IPv6Network("64:ff9b::/96"), ipaddress.IPv6Network("64:ff9b:1::/48"))
# Ranges refused even where Python's ``is_global`` admits a few special-purpose addresses in them
# (e.g. 192.0.0.9 PCP anycast, 2001:1::1 port control): no publisher is hosted there.
_ALWAYS_BLOCKED: Final = (
    ipaddress.IPv4Network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.IPv6Network("2001::/23"),  # IETF protocol assignments (Teredo included)
)


class FetchRefused(Exception):
    """Nothing was fetched (or the response was discarded). ``code`` is for programs."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        location: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.location = location  # where a redirect pointed, when the fetch stopped at one
        self.retry_after = retry_after  # seconds the publisher asked us to wait (Retry-After on 429 or 503)


@dataclass(frozen=True)
class FetchResult:
    """One fetched resource, with what is needed to cite and verify it."""

    url: str
    final_url: str
    status: int
    content: bytes
    content_type: str | None
    sha256: str
    retrieved_at: datetime  # UTC


# ---------------------------------------------------------------------------
# Link and address checks
# ---------------------------------------------------------------------------


def validate_url(url: str, allowed_hosts: frozenset[str]) -> SplitResult:
    """Parse ``url``; refuse anything but a plain HTTPS link to an allowed host."""
    text = url.strip()
    if not text:
        raise FetchRefused("invalid_url", "The link is empty.")
    if len(text) > MAX_URL_LENGTH:
        raise FetchRefused("invalid_url", "The link is too long.")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in text):
        raise FetchRefused("invalid_url", "The link has spaces or hidden characters in it.")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as e:
        raise FetchRefused("invalid_url", "That is not a valid web link.") from e
    if parts.scheme.lower() != "https":
        raise FetchRefused("insecure_scheme", "Only secure links (https://) are fetched.")
    if not parts.hostname:
        raise FetchRefused("invalid_url", "The link has no host name in it.")
    if parts.username is not None or parts.password is not None:
        raise FetchRefused("invalid_url", "Links with a user name or password in them are not fetched.")
    if port is not None and port != HTTPS_PORT:
        raise FetchRefused("invalid_url", "Only standard https:// links are fetched (no port number).")
    host = parts.hostname.lower()
    if host not in {h.lower() for h in allowed_hosts}:
        raise FetchRefused(
            "host_not_allowed", f"{host} is not a catalogued publisher host, so it is not fetched."
        )
    return parts


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally routable unicast address (never this machine or a private network)."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: ipaddress.IPv4Address | None = ip.ipv4_mapped
        if embedded is None and any(ip in net for net in _NAT64_PREFIXES):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is None and int(ip) >> 32 == 0:  # IPv4-compatible (::a.b.c.d), deprecated
            return False
        if embedded is not None:
            return is_public_address(embedded)
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
    if any(ip in net for net in _ALWAYS_BLOCKED):
        return False
    return ip.is_global and not (
        ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private
    )


def _resolve(host: str) -> list[str]:
    """Every address a host name resolves to, for TCP on the HTTPS port."""
    try:
        infos = socket.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as e:
        raise FetchRefused("dns_failed", f"The host {host} could not be found.") from e
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def public_addresses(host: str) -> list[str]:
    """Resolve ``host``; refuse it unless every address is public (a mixed answer could be steered)."""
    raw = _resolve(host)
    if not raw:
        raise FetchRefused("dns_failed", f"The host {host} could not be found.")
    for text in raw:
        try:
            ip = ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError as e:
            raise FetchRefused(
                "blocked_address", f"{host} did not resolve to a public internet address."
            ) from e
        if not is_public_address(ip):
            raise FetchRefused(
                "blocked_address", f"{host} resolves to a private or reserved address; not fetched."
            )
    return raw


def _check_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    checked: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if not _HEADER_NAME.match(name) or name.lower() in _RESERVED_HEADERS:
            raise FetchRefused("bad_header", f"The header {name!r} can't be set by the caller.")
        if any(ch in value for ch in "\r\n\x00"):
            raise FetchRefused("bad_header", f"The header {name!r} has a line break in its value.")
        checked[name] = value
    return checked


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Proxy:
    host: str
    port: int


def _proxy_from_env() -> _Proxy | None:
    value = os.environ.get(PROXY_ENV, "").strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as e:
        raise FetchRefused("proxy_config", f"{PROXY_ENV} is not a valid proxy address.") from e
    if parts.scheme.lower() != "http" or not parts.hostname or parts.username or parts.password:
        raise FetchRefused("proxy_config", f"{PROXY_ENV} must look like http://host:port (no credentials).")
    return _Proxy(parts.hostname, port or 80)


EXTRA_CA_ENV: Final = "HDR_EXTRA_CA_FILE"
_proxy_warned = False


def _warn_proxy_once() -> None:
    global _proxy_warned  # noqa: PLW0603 - one warning per process
    if not _proxy_warned:
        _proxy_warned = True
        logger.warning(
            "%s is set: fetches go through that proxy, which does its own DNS lookup, so the private-address "
            "check is advisory and %s (if set) is trusted for every publisher",
            PROXY_ENV,
            EXTRA_CA_ENV,
        )


def _tls_context(*, proxied: bool) -> ssl.SSLContext:
    """Verifying TLS context (chain and host name), TLS 1.2 or newer."""
    context = ssl.create_default_context()
    if proxied:
        path = os.environ.get(EXTRA_CA_ENV)
        if path and os.path.isfile(path):
            context.load_verify_locations(cafile=path)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _open_tcp(address: str, port: int, timeout: float) -> socket.socket:
    """TCP connection to one already-checked address (or the configured proxy)."""
    return socket.create_connection((address, port), timeout=timeout)


class _ProxyError(OSError):
    """The proxy refused or broke the CONNECT tunnel."""


def _open_tunnel(proxy: _Proxy, host: str, timeout: float) -> socket.socket:
    """A TCP stream to ``host``:443 through an HTTP CONNECT proxy, naming the host (not an address)."""
    sock = _open_tcp(proxy.host, proxy.port, timeout)
    try:
        sock.sendall(
            f"CONNECT {host}:{HTTPS_PORT} HTTP/1.1\r\nHost: {host}:{HTTPS_PORT}\r\n\r\n".encode("ascii")
        )
        reply = b""
        while not reply.endswith(b"\r\n\r\n"):  # byte by byte: nothing past the reply is consumed
            byte = sock.recv(1)
            if not byte:
                raise _ProxyError("the proxy closed the connection")
            reply += byte
            if len(reply) > MAX_PROXY_REPLY_BYTES:
                raise _ProxyError("the proxy's reply is too long")
        status_line = reply.split(b"\r\n", 1)[0].decode("latin-1")
        fields = status_line.split(None, 2)
        if len(fields) < 2 or not fields[0].startswith("HTTP/") or fields[1] != "200":
            raise _ProxyError(f"the proxy answered {status_line[:60]!r}")
    except BaseException:
        sock.close()
        raise
    return sock


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a host name over a TCP stream chosen here: a checked address, or a CONNECT tunnel.

    The Host header, SNI and certificate check all use the host name.
    """

    def __init__(
        self, host: str, addresses: list[str], proxy: _Proxy | None, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, HTTPS_PORT, timeout=timeout, context=context)
        self._addresses = addresses
        self._proxy = proxy
        self._tls = context
        # http.client drops ``self.sock`` once a Connection: close response is handed out; the
        # watchdog and the per-read timeouts still need the socket the body is read from.
        self.live_socket: socket.socket | None = None

    def _raw(self) -> socket.socket:
        timeout = float(self.timeout or CONNECT_TIMEOUT_S)
        if self._proxy is not None:
            return _open_tunnel(self._proxy, self.host, timeout)
        last_error: OSError | None = None
        for address in self._addresses:
            try:
                return _open_tcp(address, HTTPS_PORT, timeout)
            except OSError as e:
                last_error = e
        raise last_error or OSError("no address to connect to")

    def connect(self) -> None:
        raw = self._raw()
        self.live_socket = raw  # the watchdog can cut a stalled TLS handshake too
        try:
            self.sock = self.live_socket = self._tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------


def _too_large(max_bytes: int) -> FetchRefused:
    return FetchRefused(
        "too_large", f"The response is larger than the {max_bytes:,}-byte limit; it was not kept."
    )


def _timed_out() -> FetchRefused:
    return FetchRefused("timeout", "The publisher took too long to answer, so the fetch was stopped.")


def _damaged() -> FetchRefused:
    return FetchRefused("damaged", "The response arrived damaged (it could not be decompressed).")


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        left = self._end - time.monotonic()
        if left <= 0:
            raise _timed_out()
        return left


class _Watchdog:
    """Cuts the connection when the overall deadline passes, whatever a read is waiting on."""

    def __init__(self, connection: _GuardedHTTPSConnection, seconds: float) -> None:
        self.fired = False
        self._connection = connection
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        self.fired = True
        sock = self._connection.live_socket
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:  # already closed by the fetch itself
            logger.debug("fetch watchdog: socket already closed")

    def cancel(self) -> None:
        self._timer.cancel()


_GZIP_WBITS: Final = 16 + zlib.MAX_WBITS


class _BoundedBody:
    """Collects the body, inflating gzip/deflate, never past ``max_bytes`` on the wire or inflated."""

    def __init__(self, content_encoding: str | None, max_bytes: int) -> None:
        encoding = (content_encoding or "identity").strip().lower()
        self._max = max_bytes
        self._wbits: int | None
        if encoding in ("", "identity"):
            self._wbits = None
        elif encoding in ("gzip", "x-gzip"):
            self._wbits = _GZIP_WBITS
        elif encoding == "deflate":
            self._wbits = zlib.MAX_WBITS  # zlib-wrapped; raw deflate is tried if the header is wrong
        else:
            raise FetchRefused(
                "bad_encoding", f"The publisher sent an encoding this fetcher can't read ({encoding})."
            )
        self._inflater = zlib.decompressobj(wbits=self._wbits) if self._wbits is not None else None
        self._parts: list[bytes] = []
        self._size = 0
        self._wire = 0
        self._head = b""  # deflate input seen before any output, replayed if it turns out raw

    def _keep(self, data: bytes) -> None:
        self._size += len(data)
        if self._size > self._max:
            raise _too_large(self._max)
        if data:
            self._parts.append(data)

    def feed(self, chunk: bytes) -> None:
        self._wire += len(chunk)
        if self._wire > self._max:
            raise _too_large(self._max)
        if self._inflater is None:
            self._keep(chunk)
            return
        guessing_deflate = self._wbits == zlib.MAX_WBITS and self._size == 0
        try:
            self._inflate(chunk)
        except zlib.error as e:
            if not guessing_deflate:
                raise _damaged() from e
            # "deflate" sent without the zlib header (some servers do): start again as raw deflate.
            self._wbits = -zlib.MAX_WBITS
            self._inflater = zlib.decompressobj(wbits=self._wbits)
            replay, self._head = self._head + chunk, b""
            try:
                self._inflate(replay)
            except zlib.error as again:
                raise _damaged() from again
            return
        if guessing_deflate:
            self._head = self._head + chunk if self._size == 0 else b""

    def _inflate(self, data: bytes) -> None:
        inflater = self._inflater
        if inflater is None or self._wbits is None:  # pragma: no cover - only called when inflating
            raise _damaged()
        pending = data
        while pending:
            if inflater.eof:
                # gzip allows several members back to back (bgzip, pigz, concatenated files).
                if self._wbits != _GZIP_WBITS:
                    raise _damaged()
                inflater = self._inflater = zlib.decompressobj(wbits=self._wbits)
            # Never ask for more than one byte past the cap, so a bomb stops early.
            self._keep(inflater.decompress(pending, self._max - self._size + 1))
            pending = inflater.unconsumed_tail or inflater.unused_data

    def finish(self) -> bytes:
        if self._inflater is not None:
            if not self._inflater.eof:
                raise FetchRefused("damaged", "The response arrived incomplete.")
            self._keep(self._inflater.flush())
        return b"".join(self._parts)


def _read_body(
    response: http.client.HTTPResponse, sock: socket.socket | None, deadline: _Deadline, max_bytes: int
) -> bytes:
    length = response.getheader("Content-Length")
    if length is not None and length.strip().isdigit() and int(length) > max_bytes:
        raise _too_large(max_bytes)
    body = _BoundedBody(response.getheader("Content-Encoding"), max_bytes)
    while not response.isclosed():  # http.client closes it once the whole body is read
        if sock is not None and sock.fileno() != -1:
            sock.settimeout(min(READ_TIMEOUT_S, deadline.remaining()))
        chunk = response.read(CHUNK_BYTES)
        if not chunk:
            break
        body.feed(chunk)
        deadline.remaining()
    return body.finish()


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """A Retry-After header in seconds: delay-seconds or an HTTP date (RFC 9110 10.2.3); None if unreadable."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():  # "²" or "٣" are digits to isdigit() but not to float()
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    max_bytes: int = 50_000_000,
    timeout: float = 60.0,
    headers: Mapping[str, str] | None = None,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchResult:
    """GET ``url`` from a catalogued host over HTTPS, following checked redirects. See the module doc.

    Args:
        url: the link from the catalog.
        allowed_hosts: host names this fetch (and its redirects) may reach.
        max_bytes: cap on the body, on the wire and after decompression.
        timeout: deadline for the whole fetch, redirects included, in seconds.
        headers: extra request headers (e.g. an API app token), sent only to the original host.
        max_redirects: redirects to follow (at most ``MAX_REDIRECTS``). A polite crawler passes 0 so one call
            is exactly one request; a redirect then raises ``too_many_redirects`` with its ``location``.

    Raises:
        FetchRefused: with a ``code`` and a plain-language ``message``.
        ValueError: ``max_bytes`` or ``timeout`` is not positive.
    """
    if max_bytes < 1:
        raise ValueError(f"max_bytes must be positive, got {max_bytes!r}")
    if not timeout > 0:
        raise ValueError(f"timeout must be positive, got {timeout!r}")
    if not 0 <= max_redirects <= MAX_REDIRECTS:
        raise ValueError(f"max_redirects must be between 0 and {MAX_REDIRECTS}, got {max_redirects!r}")
    extra = _check_headers(headers)
    proxy = _proxy_from_env()
    if proxy is not None:
        _warn_proxy_once()
    deadline = _Deadline(timeout)
    context = _tls_context(proxied=proxy is not None)
    original = url.strip()
    origin_host = validate_url(original, allowed_hosts).hostname
    current = original
    for hop in range(max_redirects + 1):
        parts = validate_url(current, allowed_hosts)
        host = (parts.hostname or "").lower()
        addresses = public_addresses(host)
        connection = _GuardedHTTPSConnection(
            host, addresses, proxy, timeout=min(CONNECT_TIMEOUT_S, deadline.remaining()), context=context
        )
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
            **(extra if host == origin_host else {}),
        }
        watchdog = _Watchdog(connection, deadline.remaining())
        response: http.client.HTTPResponse | None = None
        try:
            connection.request("GET", target, headers=request_headers)
            if connection.live_socket is not None:
                connection.live_socket.settimeout(min(READ_TIMEOUT_S, deadline.remaining()))
            response = connection.getresponse()
            status = response.status
            if status in REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location:
                    raise FetchRefused(
                        "http_error", f"The publisher answered {status} with nowhere to go.", status=status
                    )
                current = urljoin(current, location.strip())
                if hop == max_redirects:
                    raise FetchRefused(
                        "too_many_redirects",
                        f"The link redirected more than {max_redirects} times, so it was stopped.",
                        status=status,
                        location=current,
                    )
                logger.info("fetch redirect: host=%s status=%d hop=%d", host, status, hop)
                continue
            if status != 200:
                raise FetchRefused(
                    "http_error",
                    f"The publisher answered with status {status}.",
                    status=status,
                    retry_after=retry_after_seconds(response.getheader("Retry-After"))
                    if status in RETRY_AFTER_STATUSES
                    else None,
                )
            content_type = response.getheader("Content-Type")
            content = _read_body(response, connection.live_socket, deadline, max_bytes)
            if watchdog.fired:  # the read ended because the watchdog cut it, not at the end
                raise _timed_out()
        except FetchRefused:
            if watchdog.fired:
                raise _timed_out() from None
            raise
        except _ProxyError as e:
            raise FetchRefused("proxy_error", f"The egress proxy refused the connection to {host}.") from e
        except ssl.SSLError as e:
            if watchdog.fired:
                raise _timed_out() from e
            raise FetchRefused(
                "tls_error", f"The certificate of {host} could not be verified; nothing was fetched."
            ) from e
        except TimeoutError as e:
            raise _timed_out() from e
        except (OSError, ValueError, http.client.HTTPException) as e:  # ValueError: read on a cut socket
            if watchdog.fired:
                raise _timed_out() from e
            raise FetchRefused("connect_failed", f"The connection to {host} failed.") from e
        finally:
            watchdog.cancel()
            if response is not None:
                response.close()
            connection.close()

        digest = hashlib.sha256(content).hexdigest()
        logger.info(
            "fetched: host=%s bytes=%d redirects=%d proxied=%s", host, len(content), hop, proxy is not None
        )
        return FetchResult(
            url=original,
            final_url=current,
            status=status,
            content=content,
            content_type=content_type,
            sha256=digest,
            retrieved_at=datetime.now(UTC),
        )

    raise FetchRefused(  # pragma: no cover - the last hop raises inside the loop
        "too_many_redirects", f"The link redirected more than {max_redirects} times, so it was stopped."
    )
