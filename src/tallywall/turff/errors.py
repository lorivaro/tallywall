"""Exceptions raised by Turff backends.

`retryable` tells the outbox whether trying again later can help.
"""

from __future__ import annotations


class TurffError(Exception):
    retryable: bool = False


class TurffAuthError(TurffError):
    """Login rejected, or still unauthorised after one re-login."""


class TurffAPIError(TurffError):
    """Turff understood the request and refused it (e.g. ERR_HOUSE_USER_IS_NOT_MANAGER)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_code: str | None = None,
        log_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.log_id = log_id

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.error_code:
            parts.append(f"code={self.error_code}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.log_id:
            parts.append(f"logId={self.log_id}")
        return " ".join(parts)


class TurffUnavailable(TurffError):
    """Network error, timeout or 5xx: Turff could not be reached right now."""

    retryable = True
