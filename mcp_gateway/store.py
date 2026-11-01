"""Durable storage for servers registered at runtime through the admin API."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .models import ServerSpec


class RegistryStore:
    def __init__(self, path: str | None) -> None:
        self._lock = threading.Lock()
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL") if path and path != ":memory:" else None
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS servers (
                   name TEXT PRIMARY KEY,
                   spec_json TEXT NOT NULL,
                   created_by TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL)"""
        )
        self._db.commit()

    def upsert(self, spec: ServerSpec, actor: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._db.execute(
                """INSERT INTO servers(name, spec_json, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET spec_json=excluded.spec_json, updated_at=excluded.updated_at""",
                (spec.name, spec.model_dump_json(by_alias=True), actor, now, now),
            )
            self._db.commit()

    def delete(self, name: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM servers WHERE name = ?", (name,))
            self._db.commit()

    def load_all(self) -> list[ServerSpec]:
        with self._lock:
            rows = self._db.execute("SELECT spec_json FROM servers ORDER BY name").fetchall()
        return [ServerSpec.model_validate(json.loads(r[0])) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
