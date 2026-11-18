"""A run's files: content-addressed artifacts that verify themselves, and records."""

from __future__ import annotations

from pathlib import Path

import pytest

from msa.core.ids import Digest
from msa.runtime.artifacts import ArtifactMissing, RunFiles, digest_of


def test_artifacts_are_addressed_by_their_content(tmp_path: Path) -> None:
    files = RunFiles.for_run(tmp_path, "run-1")
    digest = files.put("plan text")
    assert digest == digest_of(b"plan text")
    assert files.put(b"plan text") == digest  # idempotent
    assert files.text(digest) == "plan text"
    assert (tmp_path / "runs" / "run-1" / "artifacts" / digest.removeprefix("sha256:")).exists()


def test_a_missing_or_corrupt_artifact_is_refused(tmp_path: Path) -> None:
    files = RunFiles(tmp_path)
    with pytest.raises(ArtifactMissing, match="no artifact"):
        files.get(digest_of(b"never stored"))
    digest = files.put("original")
    (tmp_path / "artifacts" / digest.removeprefix("sha256:")).write_bytes(b"tampered")
    with pytest.raises(ArtifactMissing, match="corrupt"):
        files.get(digest)
    with pytest.raises(ArtifactMissing, match="not an artifact digest"):
        files.get(Digest("sha256:../../etc/passwd"))


def test_records_append_and_survive_a_torn_line(tmp_path: Path) -> None:
    files = RunFiles(tmp_path)
    assert files.records("log.jsonl") == []
    files.record("log.jsonl", {"n": 1})
    files.record("log.jsonl", {"n": 2})
    with (tmp_path / "log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"n": 3')  # a crash mid-write
    assert files.records("log.jsonl") == [{"n": 1}, {"n": 2}]


def test_named_files_round_trip(tmp_path: Path) -> None:
    files = RunFiles(tmp_path / "deep")
    assert files.read("report.md") is None
    path = files.write("report.md", "# Run\n")
    assert path.read_text(encoding="utf-8") == "# Run\n"
    assert files.read("report.md") == "# Run\n"
