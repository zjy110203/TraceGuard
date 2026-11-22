"""Unified error model and API error envelope (v1.1 stage 0 tasks 4 & 9).

A single error shape is the foundation for the C-contracts later. Domain errors
carry a stable ``code`` so callers can branch without parsing messages.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for expected, well-typed failures."""

    code: str = "DOMAIN_ERROR"
    status_code: int = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class NotFoundError(DomainError):
    code = "NOT_FOUND"
    status_code = 404


class ConflictError(DomainError):
    code = "CONFLICT"
    status_code = 409


class ValidationError(DomainError):
    code = "VALIDATION_ERROR"
    status_code = 422


class UnauthorizedError(DomainError):
    code = "UNAUTHORIZED"
    status_code = 401


class ForbiddenError(DomainError):
    code = "FORBIDDEN"
    status_code = 403
