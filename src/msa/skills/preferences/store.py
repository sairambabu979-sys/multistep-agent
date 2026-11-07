"""Where the layers live, how they are read, and how the CLI writes them (PLAN §5.3, §5.5).

- built-in: ``defaults.yaml`` next to this module, shipped with the package;
- user: ``$MSA_USER_PREFERENCES``, else ``$XDG_CONFIG_HOME/msa/preferences.yaml``, else
  ``~/.config/msa/preferences.yaml``;
- repo: ``<root>/.msa/preferences.yaml``;
- run: ``--pref <id>=<enforcement>`` flags.

Files are read with a safe loader that also rejects duplicate keys, which a plain YAML
loader would silently resolve to the last one. Writes keep the file's comments and layout
(round-trip mode) and are atomic. A write is refused unless every layer, with the change
applied, still resolves, so ``msa prefs add`` can never save a set that would fail INTAKE.
"""

from __future__ import annotations

import io
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from msa.skills.preferences.resolve import (
    Layer,
    LayerName,
    PreferenceError,
    Resolved,
    resolve,
    run_layer,
)
from msa.skills.preferences.schema import SCHEMA_VERSION, Preference

REPO_FILE = Path(".msa") / "preferences.yaml"
PROPOSALS = Path(".msa") / "proposals"


def user_path() -> Path:
    if explicit := os.environ.get("MSA_USER_PREFERENCES"):
        return Path(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "msa" / "preferences.yaml"


def _safe() -> YAML:
    return YAML(typ="safe", pure=True)


def _round_trip() -> YAML:
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    return yaml


def _read(path: Path) -> object:
    try:
        return _safe().load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise PreferenceError(f"{path}: not valid YAML: {exc}") from None


def builtin_layer() -> Layer:
    text = resources.files(__package__).joinpath("defaults.yaml").read_text(encoding="utf-8")
    return Layer.from_document("built-in", "built-in defaults", _safe().load(text))


def file_layer(name: LayerName, path: Path) -> Layer | None:
    if not path.exists():
        return None
    return Layer.from_document(name, str(path), _read(path))


def load_layers(
    root: Path, overrides: Iterable[str] = (), *, user: Path | None = None
) -> list[Layer]:
    files: tuple[tuple[LayerName, Path], ...] = (
        ("user", user or user_path()),
        ("repo", root / REPO_FILE),
    )
    layers = [builtin_layer()]
    layers += [layer for name, path in files if (layer := file_layer(name, path)) is not None]
    layers.append(run_layer(overrides))
    return layers


def load(root: Path, overrides: Iterable[str] = (), *, user: Path | None = None) -> Resolved:
    return resolve(load_layers(root, overrides, user=user))


# ---------- writing a layer


def add(
    path: Path,
    preference: Preference,
    *,
    others: Sequence[Layer],
    name: LayerName = "repo",
) -> Resolved:
    """Append ``preference`` to the layer file at ``path``, keeping its comments. Refused if
    the id is already in that layer, or if the layers would no longer resolve."""
    yaml = _round_trip()
    document: Any = (
        yaml.load(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"version": SCHEMA_VERSION, "preferences": []}
    )
    if document is None:
        document = {"version": SCHEMA_VERSION, "preferences": []}
    entries = document.setdefault("preferences", [])
    if any(entry.get("id") == preference.id for entry in entries):
        raise PreferenceError(f"{preference.id} is already in {path}; edit it there")
    entries.append(preference.model_dump(mode="json", exclude_defaults=True))
    out = io.StringIO()
    yaml.dump(document, out)
    text = out.getvalue()
    layer = Layer.from_document(name, str(path), _safe().load(text))  # the file as it would be
    resolved = resolve([*(o for o in others if o.name != name), layer])
    _write_atomic(path, text)
    return resolved


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


# ---------- proposals (PLAN §5.5): learned preferences wait for a human


@dataclass(frozen=True, slots=True)
class Proposal:
    path: Path
    reason: str
    preference: Preference


def proposals(root: Path) -> list[Proposal]:
    found = []
    for path in sorted((root / PROPOSALS).glob("*.yaml")):
        data: Any = _read(path)
        try:
            if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
                raise PreferenceError(f"{path}: expected version: {SCHEMA_VERSION}")
            preference = Preference.model_validate({**data["preference"], "source": "learned"})
            found.append(Proposal(path, str(data.get("reason", "")), preference))
        except (KeyError, TypeError, ValidationError) as exc:
            raise PreferenceError(f"{path}: not a valid proposal: {exc}") from None
    return found


def approve(root: Path, proposal: Proposal, *, others: Sequence[Layer]) -> Resolved:
    resolved = add(root / REPO_FILE, proposal.preference, others=others)
    proposal.path.unlink()
    return resolved


def reject(proposal: Proposal) -> None:
    proposal.path.unlink()
