"""Minimal HTTP layer built on urllib.

Using the standard library keeps the tool dependency-free and, more usefully,
lets the raw socket/TLS exceptions through so the caller can tell DNS failure
from a refused connection from a timeout from a bad certificate. Request
libraries tend to collapse all of those into one class.
"""

from __future__ import annotations

import gzip
import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any

from .errors import (
    ConnectionTimeoutError,
    HostResolutionError,
    HostUnreachableError,
    TLSError,
)
from .secrets import scrub_text

log = logging.getLogger("dropwatch.http")


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str
    url: str

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8", errors="replace"))

    @property
    def looks_like_json(self) -> bool:
        return "json" in self.content_type.lower()


class RateLimiter:
    """Token bucket. Deezer allows roughly 50 requests per 5 seconds per IP."""

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                time.sleep((1.0 - self._tokens) / self._rate)


def _decode_body(raw: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw
    return raw


class HttpClient:
    """Thread-safe HTTP GET client with a shared connection-free opener."""

    def __init__(
        self,
        timeout: float,
        user_agent: str,
        verify_tls: bool = True,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.rate_limiter = rate_limiter
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        # No redirect handler changes: urllib follows redirects by default,
        # which is what we want for reverse-proxied Navidrome installs.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context)
        )

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        """Perform a GET, translating transport failures into typed errors.

        HTTP error statuses are returned as a `Response`, not raised: both APIs
        express application errors in the body, so the caller decides.
        """
        if self.rate_limiter is not None:
            self.rate_limiter.acquire()

        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", self.user_agent)
        request.add_header("Accept", "application/json")
        request.add_header("Accept-Encoding", "gzip, deflate")
        for key, value in (headers or {}).items():
            request.add_header(key, value)

        # Never let a raw URL (which may carry an auth token) reach a log.
        safe_url = scrub_text(url)
        # Error messages quote the endpoint without its query string, which is
        # both easier to read and one less way for a credential to escape.
        display_url = url.split("?", 1)[0]
        log.debug("GET %s", safe_url)

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return Response(
                    status=response.status,
                    body=_decode_body(raw, response.headers.get("Content-Encoding", "")),
                    content_type=response.headers.get("Content-Type", ""),
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            # 4xx/5xx: keep the body, the API may explain itself there.
            raw = exc.read() if hasattr(exc, "read") else b""
            return Response(
                status=exc.code,
                body=_decode_body(raw, exc.headers.get("Content-Encoding", "") if exc.headers else ""),
                content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
                url=safe_url,
            )
        except urllib.error.URLError as exc:
            raise _translate_url_error(exc, display_url) from None
        except (TimeoutError, socket.timeout):
            raise ConnectionTimeoutError(
                f"Timed out after {self.timeout:g}s waiting for {display_url}",
                hint="Try `dropwatch config set timeout 60`, or check the host is awake.",
            ) from None
        except ssl.SSLError as exc:
            raise TLSError(f"TLS error talking to {display_url}: {exc}") from None
        except OSError as exc:
            raise HostUnreachableError(f"Network error for {display_url}: {exc}") from None


def _translate_url_error(exc: urllib.error.URLError, safe_url: str):
    """Map a URLError's underlying cause to a specific, actionable error."""
    reason = exc.reason

    if isinstance(reason, ssl.SSLCertVerificationError):
        detail = getattr(reason, "verify_message", None) or reason
        return TLSError(
            f"TLS certificate verification failed for {safe_url}: {detail}",
            hint=(
                "If Navidrome uses a self-signed certificate, use http:// over "
                "Tailscale instead, which is already encrypted."
            ),
        )
    if isinstance(reason, ssl.SSLError):
        return TLSError(f"TLS handshake failed for {safe_url}: {reason}")

    if isinstance(reason, socket.gaierror):
        return HostResolutionError(
            f"Could not resolve the hostname in {safe_url}.",
            hint=(
                "Check the hostname spelling, that Tailscale is running, and that "
                "any VPN or Tailscale connection is up."
            ),
        )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return ConnectionTimeoutError(
            f"Connection to {safe_url} timed out.",
            hint="The host may be offline or asleep, or off the tailnet.",
        )
    if isinstance(reason, ConnectionRefusedError):
        return HostUnreachableError(
            f"Connection refused by {safe_url}.",
            hint="The host is up but nothing is listening on that port. Check the url setting.",
        )
    if isinstance(reason, OSError):
        # errno 65/51: no route to host / network unreachable.
        if getattr(reason, "errno", None) in (65, 51, 101, 113):
            return HostUnreachableError(
                f"No route to {safe_url}.",
                hint="The host appears to be offline or unreachable.",
            )
        return HostUnreachableError(f"Could not connect to {safe_url}: {reason}")

    return HostUnreachableError(f"Could not connect to {safe_url}: {reason}")
