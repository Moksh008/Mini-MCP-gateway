from __future__ import annotations

from ..models import ServerSpec
from .base import Upstream
from .mcp_upstream import MCPUpstream
from .rest_upstream import OpenAPIUpstream, RestUpstream
from .static import StaticUpstream


def create_upstream(spec: ServerSpec) -> Upstream:
    if spec.kind in ("mcp_http", "mcp_stdio"):
        return MCPUpstream(spec)
    if spec.kind == "openapi":
        return OpenAPIUpstream(spec)
    if spec.kind == "rest":
        return RestUpstream(spec)
    raise ValueError(f"unsupported upstream kind: {spec.kind}")


__all__ = ["Upstream", "MCPUpstream", "RestUpstream", "OpenAPIUpstream", "StaticUpstream", "create_upstream"]
