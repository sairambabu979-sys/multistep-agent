"""A worktree with an outside world next to it, for confinement tests.

tmp/
├── outside/secret.txt       must never be read, written or listed through a tool
└── repo/                    the root
    ├── src/app.py
    ├── tests/acceptance/test_login.py     (tester-owned)
    ├── docs -> src                        in-tree symlink: fine to read through
    ├── escape -> ../outside               directory symlink leaving the tree
    ├── secret_link -> ../outside/secret.txt
    └── owned_link -> tests/acceptance/test_login.py   in-tree, into tester paths
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from msa.config import Role, SandboxConfig, load_config
from msa.tools.audit import Violation
from msa.tools.capabilities import WritePolicy
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox
from msa.tools.workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
SECRET = "the secret outside the repository\n"
APP = 'def greet(name):\n    return f"hello {name}"\n'


@dataclass
class Tree:
    base: Path
    repo: Path
    outside: Path
    violations: list[Violation] = field(default_factory=list)

    def workspace(self, role: Role = Role.CODER, **limits: object) -> Workspace:
        root = Root.at(self.repo)
        config = load_config(ROOT / "msa.toml")
        policy = WritePolicy(role, config.capabilities, root.case_insensitive)
        sandbox = config.sandbox.model_copy(update=limits)
        return Workspace(root, policy, sandbox, on_violation=self.violations.append)

    def sandbox(self, role: Role = Role.CODER, **overrides: object) -> LocalSandbox:
        config = sandbox_config(**overrides)
        return LocalSandbox(
            Root.at(self.repo), config, role=role.value, on_violation=self.violations.append
        )

    def outside_untouched(self) -> bool:
        entries = sorted(p.name for p in self.outside.iterdir())
        return entries == ["secret.txt"] and (self.outside / "secret.txt").read_text() == SECRET


def build_tree(base: Path) -> Tree:
    repo, outside = base / "repo", base / "outside"
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "acceptance").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET)
    (repo / "src" / "app.py").write_text(APP)
    (repo / "tests" / "acceptance" / "test_login.py").write_text("def test_login():\n    pass\n")
    os.symlink("src", repo / "docs")
    os.symlink("../outside", repo / "escape")
    os.symlink("../outside/secret.txt", repo / "secret_link")
    os.symlink("tests/acceptance/test_login.py", repo / "owned_link")
    return Tree(base, repo, outside)


def sandbox_config(**overrides: object) -> SandboxConfig:
    return load_config(ROOT / "msa.toml").sandbox.model_copy(update=overrides)
