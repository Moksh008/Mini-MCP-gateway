"""Gateway error hierarchy. Each error maps to a stable machine-readable code."""

from __future__ import annotations


class GatewayError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # ``detail`` is recorded in the audit log but never returned to the caller.
        self.detail = detail


class AuthenticationError(GatewayError):
    code = "unauthenticated"
    http_status = 401


class ToolNotFound(GatewayError):
    """Raised for unknown tools *and* tools the caller may not use (no existence oracle)."""

    code = "tool_not_found"
    http_status = 404


class PermissionDenied(GatewayError):
    code = "permission_denied"
    http_status = 403


class RateLimited(GatewayError):
    code = "rate_limited"
    http_status = 429

    def __init__(self, message: str, *, retry_after: float, detail: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class InvalidArguments(GatewayError):
    code = "invalid_arguments"
    http_status = 400


class UpstreamUnavailable(GatewayError):
    code = "upstream_unavailable"
    http_status = 503


class UpstreamTimeout(GatewayError):
    code = "upstream_timeout"
    http_status = 504


class UpstreamError(GatewayError):
    code = "upstream_error"
    http_status = 502


class ConfigError(GatewayError):
    code = "config_error"
    http_status = 400
