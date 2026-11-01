"""Structured audit logging.

Every decision the gateway makes (list, search, describe, call, admin change, auth failure)
is emitted as one JSON line. Arguments are recorded with sensitive keys redacted and long
values truncated, plus a SHA-256 of the canonical arguments so a specific call can be
correlated with upstream logs without storing the raw payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AuditSpec


class AuditLogger:
    def __init__(self, spec: AuditSpec) -> None:
        self._spec = spec
        self._redact = tuple(k.lower() for k in spec.redact_keys)
        self._ring: deque[dict[str, Any]] = deque(maxlen=spec.ring_size)
        self._logger = logging.getLogger(f"mcp_gateway.audit.{id(self)}")
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        self._handler: logging.Handler | None = None
        if spec.path:
            Path(spec.path).parent.mkdir(parents=True, exist_ok=True)
            self._handler = logging.handlers.RotatingFileHandler(
                spec.path, maxBytes=spec.max_bytes, backupCount=spec.backup_count, encoding="utf-8"
            )
            self._handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(self._handler)

    def close(self) -> None:
        if self._handler:
            self._logger.removeHandler(self._handler)
            self._handler.close()

    # ---- redaction -------------------------------------------------------------------
    def _is_sensitive(self, key: str) -> bool:
        k = key.lower()
        return any(s in k for s in self._redact)

    def redact(self, value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "…"
        if isinstance(value, dict):
            return {
                k: ("[REDACTED]" if self._is_sensitive(str(k)) else self.redact(v, depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, list):
            items = [self.redact(v, depth + 1) for v in value[:50]]
            if len(value) > 50:
                items.append(f"…(+{len(value) - 50} items)")
            return items
        if isinstance(value, str) and len(value) > self._spec.max_value_chars:
            return value[: self._spec.max_value_chars] + f"…(+{len(value) - self._spec.max_value_chars} chars)"
        return value

    @staticmethod
    def fingerprint(arguments: Any) -> str:
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ---- emit ------------------------------------------------------------------------
    def log(self, event: str, *, arguments: Any = None, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        if arguments is not None:
            record["args"] = self.redact(arguments)
            record["args_sha256"] = self.fingerprint(arguments)
        self._ring.append(record)
        if self._handler:
            self._logger.info(json.dumps(record, default=str, ensure_ascii=False))
        return record

    def recent(
        self, limit: int = 100, *, principal: str | None = None, event: str | None = None,
        tool: str | None = None, outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rec in reversed(self._ring):
            if principal and rec.get("principal") != principal:
                continue
            if event and rec.get("event") != event:
                continue
            if tool and rec.get("tool") != tool:
                continue
            if outcome and rec.get("outcome") != outcome:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out


class Timer:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000, 2)
