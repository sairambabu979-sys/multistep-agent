"""The API's audit log: every change it makes and every request it refuses.

One JSON line per entry in ``<state dir>/audit.jsonl``, appended and fsynced before the
response is sent: who (the token's person, or ``anonymous``), their role, what they asked,
for which run, whether it was done, and why not. Reads are not logged: a dashboard that
polls would bury the changes. Tokens never appear in it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Entry:
    principal: str
    role: str
    action: str
    run: str | None
    status: int
    detail: str = ""
    at: str = ""


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, entry: Entry) -> None:
        stamped = asdict(entry) | {"at": datetime.now(UTC).isoformat(timespec="milliseconds")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(stamped, sort_keys=True) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def entries(self, *, limit: int = 500) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:]]
