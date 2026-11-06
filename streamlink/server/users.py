"""Persistent account store with salted PBKDF2 password hashes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

from streamlink.protocol.messages import valid_username

log = logging.getLogger(__name__)

DEFAULT_ITERATIONS = 100_000


def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


class UserStore:
    """Accounts kept in a JSON file. Unknown users are registered on first login if allowed."""

    def __init__(
        self,
        path: str | os.PathLike | None,
        allow_register: bool = True,
        iterations: int = DEFAULT_ITERATIONS,
    ) -> None:
        self.path = Path(path) if path else None
        self.allow_register = allow_register
        self.iterations = iterations
        self._users: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict):
                self._users = data
        except (OSError, json.JSONDecodeError) as exc:
            log.error("cannot read user store %s: %s", self.path, exc)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._users, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("cannot write user store %s: %s", self.path, exc)

    def __contains__(self, username: str) -> bool:
        return username in self._users

    async def authenticate(self, username: object, password: object) -> tuple[bool, str]:
        """Check credentials, registering new accounts when allowed. Returns ``(ok, reason)``."""
        if not valid_username(username):
            return False, "invalid username"
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            return False, "invalid password"
        loop = asyncio.get_running_loop()
        record = self._users.get(username)
        if record is None:
            if not self.allow_register:
                return False, "unknown user"
            salt = os.urandom(16)
            digest = await loop.run_in_executor(
                None, _hash_password, password, salt, self.iterations
            )
            if username in self._users:
                return False, "account was just created, try again"
            self._users[username] = {
                "salt": salt.hex(),
                "hash": digest.hex(),
                "iterations": self.iterations,
            }
            self._save()
            log.info("registered new account %r", username)
            return True, "registered"
        digest = await loop.run_in_executor(
            None,
            _hash_password,
            password,
            bytes.fromhex(record["salt"]),
            int(record.get("iterations", self.iterations)),
        )
        if hmac.compare_digest(digest.hex(), record["hash"]):
            return True, "ok"
        return False, "invalid credentials"
