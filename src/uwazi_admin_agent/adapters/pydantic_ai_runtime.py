"""``pydantic-ai`` implementation of :class:`LlmRuntimePort` (text in, text out).

A generated script's ``llm_complete`` / ``llm_map`` helpers need plain text
calls with no tools, no deps, and no structured output — the opposite of the
generation agent. This adapter builds a minimal tool-free ``Agent`` per call
site on the model the shared :class:`LlmPort` hands out (``OllamaAdapter``),
runs it, and converts pydantic-ai's ``RunUsage`` into the port's
:class:`~uwazi_admin_agent.domain.llm_budget.LlmUsage`.

``map_batch`` asks for a JSON array reply (one entry per input item) so one
request covers many entities; a malformed reply is retried once with an
explicit correction, then raised as :class:`LlmBatchFormatError` (the caller
records those entities as failed and continues — a partial pass is fine).

Not unit-tested directly (it needs a live model); its pure parsing helper
:func:`parse_batch_reply` is.
"""

from __future__ import annotations

import json
from typing import Any, override

from loguru import logger
from pydantic_ai import Agent, UsageLimits

from uwazi_admin_agent.configuration import LLM_MAX_OUTPUT_TOKENS
from uwazi_admin_agent.domain.llm_budget import LlmUsage
from uwazi_admin_agent.ports.llm_runtime_port import (
    LlmBatchFormatError,
    LlmBatchResult,
    LlmCallResult,
    LlmRuntimePort,
)
from uwazi_agent.ports.llm_port import LlmPort

# The batched-call instruction template: numbered items in, one JSON array out.
_BATCH_PROMPT = """\
{instruction}

You are given {count} item(s) below, each introduced by its index in the form
"### ITEM <index>". Process every item independently and reply with ONLY a JSON
array of {count} strings, in index order — element i is the result for ITEM i.
No prose, no markdown fence, no explanation: just the JSON array.

{items}
"""


def parse_batch_reply(reply: str, expected: int) -> list[str]:
    """Parse a batched reply into exactly ``expected`` strings (pure).

    Tolerates a markdown fence and surrounding prose (models add them despite
    instructions) by extracting the first balanced JSON array. Every element is
    coerced to ``str`` so a model returning numbers/``null`` still yields usable
    text. Raises :class:`LlmBatchFormatError` when no array of the right length
    can be recovered.
    """
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise LlmBatchFormatError(f"batch reply is not a JSON array: {reply[:200]!r}")
    try:
        parsed: Any = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LlmBatchFormatError(f"batch reply is not valid JSON ({exc}): {reply[:200]!r}") from exc
    if not isinstance(parsed, list) or len(parsed) != expected:
        got = len(parsed) if isinstance(parsed, list) else "non-array"
        raise LlmBatchFormatError(f"batch reply has {got} entries, expected {expected}: {reply[:200]!r}")
    return [item if isinstance(item, str) else ("" if item is None else json.dumps(item)) for item in parsed]


def _usage_of(run: Any) -> LlmUsage:
    """Convert a pydantic-ai run's usage into the domain usage model."""
    usage = run.usage()
    return LlmUsage(
        requests=int(getattr(usage, "requests", 0) or 0),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


class PydanticAiLlmRuntime(LlmRuntimePort):
    """Text-in/text-out LLM calls backed by a pydantic-ai ``Agent``."""

    def __init__(self, llm_port: LlmPort, max_requests: int = 1) -> None:
        self._llm: LlmPort = llm_port
        self._max_requests = max(1, max_requests)

    @override
    def cache_identity(self) -> str:
        """Identify the underlying model, so cache entries never cross models."""
        model = str(getattr(self._llm, "model_name", "") or type(self._llm).__name__)
        return f"{type(self).__name__}:{model}"

    def _agent(self) -> Agent[None, str]:
        """A minimal tool-free agent: one prompt in, one string out."""
        return Agent(
            self._llm.get_model(),
            output_type=str,
            model_settings={"max_tokens": LLM_MAX_OUTPUT_TOKENS},
        )

    async def complete(self, instruction: str, text: str) -> LlmCallResult:
        """Run ``instruction`` over one ``text`` in a single request."""
        prompt = f"{instruction}\n\n### TEXT\n{text}"
        run = await self._agent().run(prompt, usage_limits=UsageLimits(request_limit=self._max_requests))
        output = str(getattr(run, "output", "") or "")
        return LlmCallResult(output=output, usage=_usage_of(run))

    async def map_batch(self, instruction: str, items: list[str]) -> LlmBatchResult:
        """Run ``instruction`` over ``items`` in one request; expect one JSON array reply.

        One retry with a correction note when the reply cannot be parsed; after
        that :class:`LlmBatchFormatError` propagates (the caller marks these
        entities failed and continues).
        """
        if not items:
            return LlmBatchResult(outputs=[], usage=LlmUsage())
        rendered = "\n".join(f"### ITEM {i}\n{item}" for i, item in enumerate(items))
        prompt = _BATCH_PROMPT.format(instruction=instruction, count=len(items), items=rendered)
        agent = self._agent()
        run = await agent.run(prompt, usage_limits=UsageLimits(request_limit=self._max_requests))
        usage = _usage_of(run)
        try:
            outputs = parse_batch_reply(str(getattr(run, "output", "") or ""), len(items))
            return LlmBatchResult(outputs=outputs, usage=usage)
        except LlmBatchFormatError as first:
            logger.warning("llm_map batch reply malformed ({}); retrying once", first)
            retry_prompt = f"{prompt}\n\nYour previous reply was not a usable JSON array. Reply with ONLY the JSON array."
            retry = await agent.run(retry_prompt, usage_limits=UsageLimits(request_limit=self._max_requests))
            usage = usage.add(_usage_of(retry))
            outputs = parse_batch_reply(str(getattr(retry, "output", "") or ""), len(items))
            return LlmBatchResult(outputs=outputs, usage=usage)


__all__ = ["PydanticAiLlmRuntime", "parse_batch_reply"]
