"""API tokens: who is calling, and with which role (ADR-015 §4).

A token is 32 random bytes, shown once when it is issued (``msa tokens add``) and stored
only as its SHA-256, in ``<state dir>/tokens.json`` (mode 0600). A token names one person;
the name goes on every event the API journals for them, so a token can never be named
``agent:...``. Revoking deletes the hash.

| role | can |
|---|---|
| viewer | read runs, events, the stream, run files and records, the workflow, skills, policy |
| operator | a viewer's reads; start, pause, resume, cancel and unblock runs |
| approver | an operator's actions; approve or send back plans and releases, rule, abort |
| admin | an approver's actions; read the audit log |

No role lets an agent approve: agents act through their tools, never through the API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from msa.core.values import Person

PREFIX = "msa_"
_PERSON: TypeAdapter[str] = TypeAdapter(Person)


class Role(IntEnum):
    """Ordered: each role can do everything the ones below it can."""

    VIEWER = 1
    OPERATOR = 2
    APPROVER = 3
    ADMIN = 4

    @classmethod
    def named(cls, name: str) -> Role:
        try:
            return cls[name.upper()]
        except KeyError:
            raise TokenError(
                f"{name!r} is not a role: viewer, operator, approver or admin"
            ) from None


class TokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    role: Role


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise TokenError(f"{self.path}: expected a list of tokens")
        return data

    def _write(self, entries: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def issue(self, name: str, role: Role) -> str:
        """A new token for ``name``. The token itself is returned once and never stored."""
        try:
            _PERSON.validate_python(name)
        except ValidationError:
            raise TokenError(f"{name!r} cannot hold a token: tokens are for people") from None
        entries = self._read()
        if any(e["name"] == name for e in entries):
            raise TokenError(f"{name} already has a token: revoke it first")
        token = PREFIX + secrets.token_urlsafe(32)
        entries.append(
            {
                "name": name,
                "role": role.name.lower(),
                "sha256": _digest(token),
                "created": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        self._write(entries)
        return token

    def revoke(self, name: str) -> None:
        entries = self._read()
        kept = [e for e in entries if e["name"] != name]
        if len(kept) == len(entries):
            raise TokenError(f"{name} has no token")
        self._write(kept)

    def principals(self) -> Iterator[tuple[Principal, str]]:
        for entry in self._read():
            yield Principal(entry["name"], Role.named(entry["role"])), entry["created"]

    def verify(self, token: str) -> Principal | None:
        if not token.startswith(PREFIX):
            return None
        digest = _digest(token)
        for entry in self._read():
            if hmac.compare_digest(entry["sha256"], digest):
                return Principal(entry["name"], Role.named(entry["role"]))
        return None
