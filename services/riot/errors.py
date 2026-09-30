"""Riot API errors.

Every failure the Riot client can raise is a subclass of RiotError, so callers
can catch one type. Messages never contain the API key.
"""

from __future__ import annotations


class RiotError(Exception):
    """Base class for all Riot gateway errors."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class RiotConfigError(RiotError):
    """Configuration is missing or invalid (for example RIOT_API_KEY is empty)."""


class RiotUnauthorized(RiotError):
    """HTTP 401: the request carried no usable key."""


class RiotExpiredKey(RiotUnauthorized):
    """HTTP 403: Riot rejected the key. Development keys expire every 24 hours."""

    HELP = (
        "Riot API authentication failed.\n"
        "Your development API key may have expired.\n"
        "Update RIOT_API_KEY in .env."
    )


class RiotNotFound(RiotError):
    """HTTP 404: the player, match or resource does not exist."""


class RiotRateLimited(RiotError):
    """HTTP 429 that persisted after retries."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class RiotServerError(RiotError):
    """HTTP 5xx that persisted after retries."""


class RiotBadRequest(RiotError):
    """HTTP 400 or other unexpected 4xx: the request itself was malformed."""


class RiotNetworkError(RiotError):
    """The request never got an HTTP response (timeout, DNS, connection reset)."""


def error_for_status(status: int, url: str, body: str = "") -> RiotError:
    """Map an HTTP status to the matching RiotError."""
    detail = f" ({body[:200]})" if body else ""
    if status == 401:
        return RiotUnauthorized(
            "Riot API returned 401 Unauthorized: no API key was accepted. "
            "Check that RIOT_API_KEY is set in .env." + detail,
            status=status, url=url,
        )
    if status == 403:
        return RiotExpiredKey(RiotExpiredKey.HELP, status=status, url=url)
    if status == 404:
        return RiotNotFound(f"Riot API returned 404 Not Found for {url}", status=status, url=url)
    if status == 429:
        return RiotRateLimited("Riot API rate limit exceeded." + detail, status=status, url=url)
    if 500 <= status < 600:
        return RiotServerError(f"Riot API server error {status}." + detail, status=status, url=url)
    return RiotBadRequest(f"Riot API returned {status}." + detail, status=status, url=url)
