"""Base class for every value in the core: immutable, closed, and JSON-serializable."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Frozen(BaseModel):
    """An immutable value object.

    * ``frozen``: instances are hashable and cannot be mutated after construction, so a
      ``RunState`` can be shared, compared, and used as a dict key safely.
    * ``extra="forbid"``: unknown fields are an error, so a typo in a journal payload or a
      test fixture fails loudly instead of being silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
