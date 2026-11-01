"""API-key authentication.

Keys are stored only as SHA-256 digests. Lookup is by digest (keys are high-entropy random
tokens, so a fast hash is appropriate — this is not a password store) and the final
comparison is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from .config import ApiKeySpec, RoleSpec
from .errors import AuthenticationError
from .models import Principal

KEY_PREFIX = "mgw_"


def generate_api_key() -> tuple[str, str]:
    """Return ``(plaintext_key, sha256_hex)``. Show the plaintext once; store only the hash."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Authenticator:
    def __init__(self, api_keys: list[ApiKeySpec], roles: dict[str, RoleSpec]) -> None:
        self._roles = roles
        self._by_hash: dict[str, ApiKeySpec] = {k.key_sha256: k for k in api_keys}

    def authenticate_header(self, authorization: str | None, x_api_key: str | None = None) -> Principal:
        token: str | None = None
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() != "bearer" or not value.strip():
                raise AuthenticationError("unsupported authorization scheme; use 'Bearer <key>'")
            token = value.strip()
        elif x_api_key:
            token = x_api_key.strip()
        if not token:
            raise AuthenticationError("missing credentials")
        return self.authenticate_key(token)

    def authenticate_key(self, token: str) -> Principal:
        digest = hash_key(token)
        spec = self._by_hash.get(digest)
        if spec is None or not hmac.compare_digest(spec.key_sha256, digest) or spec.disabled:
            raise AuthenticationError("invalid credentials")
        is_admin = any(self._roles[r].admin for r in spec.roles if r in self._roles)
        return Principal(id=spec.id, roles=tuple(spec.roles), is_admin=is_admin)
