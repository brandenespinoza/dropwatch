"""Error taxonomy and process exit codes.

The PRD requires that network failures, protocol failures and credential
failures stay clearly distinguishable, so each condition gets its own class
rather than a single generic "connection failed".
"""

from __future__ import annotations


class ExitCode:
    """Process exit codes. 2 is reserved by argparse for usage errors."""

    OK = 0
    FAILURE = 1
    USAGE = 2
    CONFIG = 3
    NAVIDROME_CONNECTION = 4
    NAVIDROME_AUTH = 5
    DEEZER = 6
    PARTIAL = 7


class DropWatchError(Exception):
    """Base class. `hint` carries an actionable next step for the user."""

    exit_code = ExitCode.FAILURE

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class ConfigError(DropWatchError):
    exit_code = ExitCode.CONFIG


# --- Navidrome / transport -------------------------------------------------


class NavidromeError(DropWatchError):
    exit_code = ExitCode.NAVIDROME_CONNECTION


class HostResolutionError(NavidromeError):
    """DNS/MagicDNS could not resolve the host at all."""


class HostUnreachableError(NavidromeError):
    """Resolved, but the connection was refused or the network is down."""


class ConnectionTimeoutError(NavidromeError):
    """Connect or read timed out."""


class TLSError(NavidromeError):
    """Certificate validation failed."""


class NotSubsonicError(NavidromeError):
    """Endpoint answered, but it is not a Subsonic-compatible API."""


class BadPathError(NavidromeError):
    """Reachable server, but the configured URL path is wrong (404/HTML)."""


class UnexpectedResponseError(NavidromeError):
    """Subsonic-shaped endpoint returned something we cannot interpret."""


class NavidromeAuthError(NavidromeError):
    exit_code = ExitCode.NAVIDROME_AUTH


# --- Deezer ----------------------------------------------------------------


class DeezerError(DropWatchError):
    exit_code = ExitCode.DEEZER


class DeezerQuotaError(DeezerError):
    """Rate limit hit; retryable after a backoff."""


class DeezerUnavailableError(DeezerError):
    """Transport-level failure talking to Deezer."""

