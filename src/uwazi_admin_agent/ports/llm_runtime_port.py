"""The script-facing LLM runtime: one call, or one batched call (port).

A generated script may ask the LLM for per-entity work (extraction,
enrichment) through two bound helpers — ``llm_complete`` for a single text and
``llm_map`` for a bulk list. Both helpers funnel into this port, so the sandbox
stays decoupled from pydantic-ai and the budget ledger has exactly one place to
charge.

Why a separate port from :class:`~uwazi_agent.ports.llm_port.LlmPort`:
``LlmPort`` hands out a ``pydantic-ai`` ``Model`` for agent construction (tools,
deps, structured output), while this port is a plain text-in/text-out call
contract — the shape a sandboxed script's helper can use. The adapter bridges
them by building a minimal tool-free ``Agent`` per call site.

``LlmBatchFormatError`` is deliberately separate from a transport failure: a
malformed batch reply marks those entities failed and the pass continues, while
a transport failure is the caller's to surface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.llm_budget import LlmUsage


class LlmBatchFormatError(RuntimeError):
    """Raised when a batched call's reply cannot be parsed after one retry.

    The caller (``llm_map``) records the affected entities as failed and keeps
    going: a partial pass is fully usable, and the attempt's done/total counters
    expose the gap. Never treated as a reason to abort.
    """


class LlmCallResult(BaseModel):
    """One call's output text plus its measured usage."""

    model_config = ConfigDict(frozen=True)

    output: str = Field(description="The model's text reply.")
    usage: LlmUsage = Field(description="Real consumption of this call (requests=1).")


class LlmBatchResult(BaseModel):
    """One batched call's per-item outputs plus its measured usage."""

    model_config = ConfigDict(frozen=True)

    outputs: list[str] = Field(description="One output per input item, in input order.")
    usage: LlmUsage = Field(description="Real consumption of the whole batched call.")


class LlmRuntimePort(ABC):
    """Text-in/text-out LLM access for generated scripts."""

    def cache_identity(self) -> str:
        """Stable identity of this runtime for LLM cache keys (default: class name).

        Adapters that wrap a concrete model should include the model name so a
        cache entry produced by one model is never served to another.
        """
        return type(self).__name__

    @abstractmethod
    async def complete(self, instruction: str, text: str) -> LlmCallResult:
        """Run ``instruction`` over one ``text`` and return the reply + usage."""
        ...

    @abstractmethod
    async def map_batch(self, instruction: str, items: list[str]) -> LlmBatchResult:
        """Run ``instruction`` over ``items`` in ONE request; return one output per item.

        Raises :class:`LlmBatchFormatError` when the reply is not a JSON array of
        the right length after one retry.
        """
        ...


__all__ = ["LlmBatchFormatError", "LlmBatchResult", "LlmCallResult", "LlmRuntimePort"]
