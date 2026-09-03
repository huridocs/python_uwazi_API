"""How one parallel batch went, as Uwazi saw it.

``BatchVerdict`` is the three-way classification every ``*_parallel`` script
helper reports to the :class:`ThrottleController`; ``BatchOutcome`` is the
structured evidence behind it (per-entity counts + the verdict). "Uwazi
complained about load" reuses ``uwazi_agent``'s own working definition (see
``_categorize_publish_error`` in the ``uwazi_api`` adapter): a
``RATE_LIMITED`` error code, or an error text mentioning 429 / rate
limiting / too many requests.

- ``CLEAN`` — every entity in the batch succeeded. Evidence the current
  worker allowance is sustainable; grows the promotion streak.
- ``DEGRADED`` — per-entity failures with no load signal (validation
  rejections, missing entities, permission denials). Data problems, not
  Uwazi complaining about concurrency: the streak resets but the allowance
  is untouched.
- ``RATE_LIMITED`` — a load complaint. The allowance backs off.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class BatchVerdict(str, Enum):
    """What one parallel batch tells the throttle about Uwazi's health."""

    CLEAN = "clean"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"


class BatchOutcome(BaseModel):
    """Per-batch mutation evidence: how many writes succeeded / failed / were refused for load."""

    success_count: int = Field(default=0, description="Entities the batch applied cleanly.")
    failure_count: int = Field(default=0, description="Entities the batch failed on, for any reason.")
    rate_limited_count: int = Field(default=0, description="Entities refused with a load complaint (429/rate-limit).")
    verdict: BatchVerdict = Field(description="The batch's classification for the throttle policy.")
