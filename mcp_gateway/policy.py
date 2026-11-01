"""Role-based access control over namespaced tools.

Semantics (evaluated across all of a principal's roles):
  * A tool is permitted if at least one role allows it and no role denies it (deny wins).
  * A role with ``allow_writes: false`` only grants tools annotated ``readOnlyHint: true``.
  * Admin roles grant everything not explicitly denied.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from functools import lru_cache

from .config import RateLimitSpec, RoleSpec
from .models import Principal, ToolRecord


class PolicyEngine:
    def __init__(self, roles: dict[str, RoleSpec]) -> None:
        self._roles = roles
        # cache keyed on (roles, tool, read_only)
        self._decide = lru_cache(maxsize=65_536)(self._decide_uncached)

    def is_allowed(self, principal: Principal, tool: ToolRecord) -> bool:
        return self.explain(principal, tool)[0]

    def explain(self, principal: Principal, tool: ToolRecord) -> tuple[bool, str]:
        return self._decide(principal.roles, tool.qualified_name, tool.read_only)

    def _decide_uncached(self, roles: tuple[str, ...], name: str, read_only: bool) -> tuple[bool, str]:
        granted_by: str | None = None
        for role_name in roles:
            role = self._roles.get(role_name)
            if role is None:
                continue
            if any(fnmatchcase(name, p) for p in role.deny):
                return False, f"denied by role '{role_name}'"
            if granted_by is None:
                matches = role.admin or any(fnmatchcase(name, p) for p in role.allow)
                if matches and (role.allow_writes or read_only):
                    granted_by = role_name
        if granted_by is None:
            return False, "no role grants this tool"
        return True, f"granted by role '{granted_by}'"

    def filter(self, principal: Principal, tools: list[ToolRecord]) -> list[ToolRecord]:
        return [t for t in tools if self.is_allowed(principal, t)]

    # ---- rate limit resolution -------------------------------------------------------
    def principal_tool_rate(self, principal: Principal, default: RateLimitSpec) -> RateLimitSpec:
        """Most generous configured per-principal call rate across the principal's roles."""
        specs = [r.rate_limit for r in self._role_specs(principal) if r.rate_limit]
        return max(specs, key=lambda s: s.per_minute) if specs else default

    def tool_specific_rate(self, principal: Principal, qualified_name: str) -> RateLimitSpec | None:
        best: RateLimitSpec | None = None
        for role in self._role_specs(principal):
            for pattern, spec in role.tool_rate_limits.items():
                if fnmatchcase(qualified_name, pattern) and (best is None or spec.per_minute > best.per_minute):
                    best = spec
        return best

    def _role_specs(self, principal: Principal) -> list[RoleSpec]:
        return [self._roles[r] for r in principal.roles if r in self._roles]
