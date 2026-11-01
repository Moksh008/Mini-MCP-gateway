"""Core data models shared across the gateway."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Tool names exposed to models must satisfy the strictest common provider constraint
# (e.g. ``^[a-zA-Z0-9_-]{1,64}$``), so the gateway namespaces tools as ``<server>__<tool>``.
NAMESPACE_SEP = "__"
SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
TOOL_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def qualify(server: str, tool: str) -> str:
    """Return the gateway-qualified tool name for ``tool`` on ``server``."""
    safe = TOOL_NAME_SAFE_RE.sub("_", tool)
    return f"{server}{NAMESPACE_SEP}{safe}"[:64]


class RestParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    location: Literal["path", "query", "header", "body"] = "query"
    required: bool = False
    schema_: dict[str, Any] = Field(default_factory=lambda: {"type": "string"}, alias="schema")
    description: str = ""


class RestOperation(BaseModel):
    """A single REST endpoint exposed as an MCP tool."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str
    description: str = ""
    params: list[RestParam] = Field(default_factory=list)
    # If set, a JSON body schema; its top-level properties become tool arguments.
    body_schema: dict[str, Any] | None = None
    read_only: bool | None = None
    destructive: bool | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("method", mode="before")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v


class ServerSpec(BaseModel):
    """Registry entry describing one upstream (an MCP server or a REST/OpenAPI backend)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["mcp_http", "mcp_stdio", "rest", "openapi"]
    description: str = ""
    owner: str = ""
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    # mcp_http / openapi / rest
    url: str | None = None
    # Header values may reference environment variables with ${VAR}; they are expanded at
    # connect time so upstream secrets never live in the registry store or leak to clients.
    headers: dict[str, str] = Field(default_factory=dict)

    # mcp_stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    # rest
    operations: list[RestOperation] = Field(default_factory=list)
    # openapi: URL of the spec (defaults to <url>/openapi.json) and optional operationId filter
    openapi_url: str | None = None
    include_operations: list[str] | None = None

    timeout_s: float = Field(default=30.0, gt=0, le=600)
    max_concurrency: int = Field(default=32, ge=1, le=10_000)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not SERVER_NAME_RE.match(v) or NAMESPACE_SEP in v:
            raise ValueError(
                "server name must match ^[a-z][a-z0-9-]{1,30}$ (lowercase, digits, single dashes)"
            )
        return v

    @model_validator(mode="after")
    def _check_kind_fields(self) -> ServerSpec:
        if self.kind in ("mcp_http", "rest") and not self.url:
            raise ValueError(f"'url' is required for kind={self.kind}")
        if self.kind == "openapi" and not (self.url or self.openapi_url):
            raise ValueError("'url' or 'openapi_url' is required for kind=openapi")
        if self.kind == "mcp_stdio" and not self.command:
            raise ValueError("'command' is required for kind=mcp_stdio")
        if self.kind == "rest" and not self.operations:
            raise ValueError("at least one operation is required for kind=rest")
        return self


@dataclass(frozen=True)
class ToolDef:
    """A tool as reported by an upstream (un-namespaced)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    title: str | None = None


@dataclass(frozen=True)
class ToolRecord:
    """A tool in the gateway catalog (namespaced, enriched with registry metadata)."""

    qualified_name: str
    server: str
    upstream_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    title: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        return bool((self.annotations or {}).get("readOnlyHint", False))

    @property
    def destructive(self) -> bool:
        ann = self.annotations or {}
        # Per the MCP spec, destructiveHint defaults to true for non-read-only tools.
        if ann.get("readOnlyHint"):
            return False
        return bool(ann.get("destructiveHint", True))

    def summary(self) -> str:
        """First sentence of the description, bounded — used for compact search results."""
        text = " ".join(self.description.split())
        for stop in (". ", "\n"):
            idx = text.find(stop)
            if 0 < idx < 160:
                return text[: idx + 1]
        return text[:160] + ("…" if len(text) > 160 else "")


@dataclass(frozen=True)
class Principal:
    """An authenticated caller."""

    id: str
    roles: tuple[str, ...]
    is_admin: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallContext:
    """Per-request context used for auditing and tracing."""

    request_id: str
    principal: Principal
    transport: str = "mcp"
    discovery_mode: str = "search"
    client_ip: str | None = None
    user_agent: str | None = None
