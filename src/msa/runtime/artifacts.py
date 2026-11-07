"""A run's files: content-addressed artifacts, plus the records its report is built from.

The state machine carries digests, never contents: a ``PlanRef`` names a plan by digest, a
failed verification names its report. The contents live here, under the state directory
(``.msa/runs/<run>/``), so any executor (including one rebuilt on resume) can fetch what a
digest names, and a fetched artifact is checked against its digest.

Layout::

    .msa/runs/<run>/
        artifacts/<sha256 hex>   content-addressed: plans, verification reports, patches
        plan.json                the plan the build follows (the last one produced)
        invocations.jsonl        one line per agent invocation: role, turns, spend
        verifications.jsonl      one line per verification: tree, passed, report digest
        report.md                the run report, written when the run ends
        patch.diff               the final change, when the run completes

Writes are atomic (temporary file, fsync, rename). Records are appended one line at a time;
a crash can leave a record of an effect that then runs again, which the report tolerates.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from msa.core.ids import Digest


class ArtifactMissing(LookupError):
    pass


def digest_of(data: bytes) -> Digest:
    return Digest("sha256:" + hashlib.sha256(data).hexdigest())


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".msa-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


class RunFiles:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @classmethod
    def for_run(cls, state_dir: Path, run_id: str) -> RunFiles:
        return cls(state_dir / "runs" / run_id)

    # ---------- content-addressed artifacts

    def _artifact(self, digest: Digest) -> Path:
        algorithm, _, hexdigest = digest.partition(":")
        if algorithm != "sha256" or len(hexdigest) != 64 or not hexdigest.isalnum():
            raise ArtifactMissing(f"{digest} is not an artifact digest")
        return self.directory / "artifacts" / hexdigest

    def put(self, content: str | bytes) -> Digest:
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest = digest_of(data)
        path = self._artifact(digest)
        if not path.exists():
            write_atomic(path, data)
        return digest

    def get(self, digest: Digest) -> bytes:
        path = self._artifact(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ArtifactMissing(f"no artifact {digest} in {self.directory}") from None
        if digest_of(data) != digest:
            raise ArtifactMissing(f"artifact {digest} is corrupt")
        return data

    def text(self, digest: Digest) -> str:
        return self.get(digest).decode("utf-8")

    # ---------- named files and records

    def write(self, name: str, content: str) -> Path:
        path = self.directory / name
        write_atomic(path, content.encode("utf-8"))
        return path

    def read(self, name: str) -> str | None:
        try:
            return (self.directory / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def record(self, name: str, entry: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
        with (self.directory / name).open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def records(self, name: str) -> list[dict[str, Any]]:
        text = self.read(name)
        if text is None:
            return []
        entries = []
        for line in text.splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:  # a line torn by a crash mid-write
                continue
        return [e for e in entries if isinstance(e, dict)]
