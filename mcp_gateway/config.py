"""Gateway configuration (YAML file + environment overrides)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ConfigError
from .models import ServerSpec

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: str, *, strict: bool = True) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` references in ``value``."""

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        if strict:
            raise ConfigError(f"environment variable '{name}' is not set")
        return ""

    return _ENV_RE.sub(repl, value)


class RateLimitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_minute: float = Field(gt=0)
    burst: int | None = Field(default=None, ge=1)

    @property
    def rate_per_s(self) -> float:
        return self.per_minute / 60.0

    @property
    def capacity(self) -> float:
        return float(self.burst if self.burst is not None else max(1, int(self.per_minute)))


class RoleSpec(BaseModel):
    """RBAC role. Patterns are shell-style globs over qualified tool names (``server__tool``)."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    # When false, tools that are not annotated ``readOnlyHint: true`` are denied.
    allow_writes: bool = True
    admin: bool = False
    rate_limit: RateLimitSpec | None = None
    tool_rate_limits: dict[str, RateLimitSpec] = Field(default_factory=dict)


class ApiKeySpec(BaseModel):
    """An API-key principal. Only the SHA-256 of the key is stored."""

    model_config = ConfigDict(extra="forbid")

    id: str
    key_sha256: str
    roles: list[str]
    description: str = ""
    disabled: bool = False

    @field_validator("key_sha256")
    @classmethod
    def _hex(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("key_sha256 must be a 64-char hex SHA-256 digest")
        return v


class AuditSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = "var/audit.jsonl"
    max_bytes: int = 50 * 1024 * 1024
    backup_count: int = 5
    ring_size: int = 2000
    redact_keys: list[str] = Field(
        default_factory=lambda: [
            "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
            "card_number", "cvv", "ssn", "pin", "private_key", "credential",
        ]
    )
    max_value_chars: int = 256


class LimitsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Coarse per-principal limit for every authenticated HTTP request (list/search/call).
    default_request_rate: RateLimitSpec = RateLimitSpec(per_minute=600, burst=100)
    # Default per-principal tool-call limit if the role does not specify one.
    default_tool_call_rate: RateLimitSpec = RateLimitSpec(per_minute=120, burst=30)
    max_result_bytes: int = 256 * 1024
    max_argument_bytes: int = 64 * 1024
    search_max_results: int = 20
    # Key rate limits by principal *and* client IP. Essential when credentials are shared,
    # e.g. a public demo where everyone uses the same demo keys.
    per_client_ip: bool = False


class DemoSpec(BaseModel):
    """Public-sandbox mode: safe to expose on the internet with published demo keys."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Periodically restore the registry to the config file (drop runtime servers, re-enable all).
    reset_interval_s: float = Field(default=1800.0, ge=60)
    # Runtime-registered servers expire after this long, and at most this many may exist.
    runtime_server_ttl_s: float = Field(default=900.0, ge=30)
    max_runtime_servers: int = Field(default=3, ge=0)
    # Do not reveal other visitors' IP addresses through the audit API.
    hide_client_ips: bool = True


class AgentSpec(BaseModel):
    """Server-side LLM agent that uses the gateway as its only tool source.

    Speaks the OpenAI-compatible chat-completions protocol (OpenRouter by default). The API
    key is read from the environment and never sent to the browser. Budgets protect the
    key's owner from abuse when the demo is public.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True  # effective only when the API key env var is set
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    model: str = "anthropic/claude-haiku-5.5"
    allowed_models: list[str] = Field(default_factory=lambda: [
        "anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5",
    ])
    max_steps: int = Field(default=8, ge=1, le=25)
    max_tokens: int = Field(default=1024, ge=64, le=8192)
    timeout_s: float = Field(default=60.0, gt=0, le=300)
    max_tool_result_chars: int = Field(default=4000, ge=200)
    run_rate: RateLimitSpec = RateLimitSpec(per_minute=4, burst=3)
    daily_run_budget: int = Field(default=500, ge=1)
    daily_token_budget: int = Field(default=3_000_000, ge=1000)
    app_url: str = "https://github.com/Moksh008/mini-mcp-gateway"
    app_name: str = "Mini MCP Gateway"


class CircuitBreakerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = 5
    reset_after_s: float = 30.0


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8787
    registry_db: str | None = "var/registry.db"
    # Admin API may register mcp_stdio servers only if explicitly allowed: launching an
    # arbitrary command is remote code execution on the gateway host.
    allow_stdio_registration: bool = False
    # If non-empty, upstream URLs registered at runtime must match one of these hostnames.
    upstream_host_allowlist: list[str] = Field(default_factory=list)
    metrics_public: bool = True
    refresh_interval_s: float = Field(default=300.0, ge=0)

    servers: list[ServerSpec] = Field(default_factory=list)
    roles: dict[str, RoleSpec] = Field(default_factory=dict)
    api_keys: list[ApiKeySpec] = Field(default_factory=list)
    audit: AuditSpec = AuditSpec()
    limits: LimitsSpec = LimitsSpec()
    circuit_breaker: CircuitBreakerSpec = CircuitBreakerSpec()
    demo: DemoSpec = DemoSpec()
    agent: AgentSpec = AgentSpec()

    def model_post_init(self, _: Any) -> None:
        names = [s.name for s in self.servers]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ConfigError(f"duplicate server names: {sorted(dupes)}")
        for key in self.api_keys:
            missing = [r for r in key.roles if r not in self.roles]
            if missing:
                raise ConfigError(f"api key '{key.id}' references unknown roles {missing}")
        ids = [k.id for k in self.api_keys]
        if len(ids) != len(set(ids)):
            raise ConfigError("duplicate api key ids")


def _read_file(p: Path) -> dict[str, Any]:
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be a mapping")
    # Relative paths (stdio cwd, db, audit log) resolve against *this* file's directory.
    base = p.resolve().parent
    for server in data.get("servers", []) or []:
        # Endpoint URLs may use ${VAR:-default} so one file works across environments.
        # (Header values are deliberately expanded later, at connect time.)
        for key in ("url", "openapi_url"):
            if isinstance(server.get(key), str):
                server[key] = expand_env(server[key])
        if server.get("cwd") and not Path(server["cwd"]).is_absolute():
            server["cwd"] = str((base / server["cwd"]).resolve())
    if data.get("registry_db") and not Path(data["registry_db"]).is_absolute():
        data["registry_db"] = str((base / data["registry_db"]).resolve())
    audit = data.get("audit") or {}
    if audit.get("path") and not Path(audit["path"]).is_absolute():
        audit["path"] = str((base / audit["path"]).resolve())
    return data


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings; lists and scalars in ``overlay`` replace those in ``base``."""
    out = dict(base)
    for k, v in overlay.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(paths: str | os.PathLike[str] | list | None = None) -> GatewayConfig:
    """Load one or more YAML files (later files overlay earlier ones), then env overrides.

    ``paths`` may be a path, a list of paths, or None to use ``$MCPGW_CONFIG`` (comma-separated).
    """
    if paths is None:
        env = os.environ.get("MCPGW_CONFIG", "")
        paths = [x.strip() for x in env.split(",") if x.strip()]
    elif not isinstance(paths, list):
        paths = [paths]
    data: dict[str, Any] = {}
    for path in paths:
        data = deep_merge(data, _read_file(Path(path)))
    if os.environ.get("MCPGW_HOST"):
        data["host"] = os.environ["MCPGW_HOST"]
    if os.environ.get("MCPGW_PORT"):
        data["port"] = int(os.environ["MCPGW_PORT"])
    try:
        return GatewayConfig.model_validate(data)
    except ConfigError:
        raise
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(f"invalid gateway config: {e}") from e
