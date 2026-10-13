"""Encryption at rest for the few secrets we must keep (a test-login cookie, a GitHub user token).

Fernet (AES-128-CBC + HMAC-SHA256). The key comes from SECRETS_KEY and is never stored with the data. Ciphertext is
tagged with a version prefix so the key or algorithm can be rotated later without a flag day.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "v1:"


class SecretsError(RuntimeError):
    pass


class Vault:
    def __init__(self, key: str):
        if not key:
            raise SecretsError("SECRETS_KEY is not set; refusing to store secrets unencrypted")
        try:
            self._f = Fernet(key.encode())
        except (ValueError, TypeError) as e:
            raise SecretsError("SECRETS_KEY is not a valid Fernet key") from e

    def seal(self, value: object) -> str:
        return PREFIX + self._f.encrypt(json.dumps(value).encode()).decode()

    def open(self, sealed: str) -> object:
        if not sealed.startswith(PREFIX):
            raise SecretsError("unknown secret format")
        try:
            return json.loads(self._f.decrypt(sealed[len(PREFIX):].encode()))
        except InvalidToken as e:
            raise SecretsError("could not decrypt: wrong SECRETS_KEY, or the data was altered") from e
