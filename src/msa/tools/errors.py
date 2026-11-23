"""Errors a tool reports to the model.

Each is raised by the tool layer and returned to the model as a ``tool_result`` with
``is_error: true`` (the gateway turns an exception into ``"<Class>: <message>"``). The run
continues. Messages name paths relative to the worktree, never host paths.
"""

from __future__ import annotations


class ToolError(Exception):
    """A request the tool refused or could not complete. The model can recover from it."""


class PathRejected(ToolError):
    """The path is malformed, absolute, or resolves outside the worktree."""


class PathChanged(ToolError):
    """The path changed between resolving and opening it (a symlink appeared): refused."""


class CapabilityDenied(ToolError):
    """The role may not write this path (ADR-006)."""


class FileMissing(ToolError):
    pass


class NotAFile(ToolError):
    """A directory, FIFO, device or socket where a regular file was expected."""


class FileExists(ToolError):
    pass


class TooLarge(ToolError):
    pass


class NotText(ToolError):
    """The file is not UTF-8 text."""


class StaleRead(ToolError):
    """The file changed since the model read it: read it again before editing."""


class EditMismatch(ToolError):
    """``old`` does not occur exactly once."""


class CommandRejected(ToolError):
    """The command or one of its arguments is not allowed."""


class CommandUnavailable(ToolError):
    """The program is not installed on the sandbox's PATH."""
