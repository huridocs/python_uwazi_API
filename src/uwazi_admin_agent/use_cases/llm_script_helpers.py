"""The ``llm_complete`` / ``llm_map`` helpers bound into a script's namespace.

A generated script does per-entity LLM work (extraction, enrichment) through
these two sync helpers. Each helper:

1. estimates the call's size and lets the :class:`LlmLedger` check it against
   the operator-approved budget — a breach raises
   :class:`~uwazi_admin_agent.domain.llm_budget.LlmBudgetExceededError` BEFORE
   the request is issued, so a runaway pass stops at the ceiling instead of
   after it;
2. issues the call on the script's dedicated event loop;
3. records the call's REAL usage in the ledger (which catches an under-estimate
   on the next check);
4. reports progress so the pass's done/total counters advance.

Both helpers behave identically in dry-run and real mode: they are pure
text-in/text-out, so a prediction pass and its execute pass see the same
outputs (given the same model temperature-free behaviour) and the rehearsal is
faithful. Unwired helpers raise loudly, matching the namespace's other unwired
stubs.

Token estimates are deliberately crude (``len/4`` plus a shape-based output
guess): the caps, not the estimate, are what actually bound a pass.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from loguru import logger

from uwazi_admin_agent.configuration import LLM_MAP_BATCH_SIZE
from uwazi_admin_agent.domain.llm_budget import LlmLedger, LlmUsage, estimate_tokens
from uwazi_admin_agent.domain.text_cache import llm_complete_cache_key, llm_map_cache_key
from uwazi_admin_agent.ports.llm_runtime_port import LlmBatchFormatError, LlmRuntimePort
from uwazi_admin_agent.ports.text_cache_port import TextCachePort


def selection_llm_refusal() -> dict[str, Any]:
    """The ``llm_complete`` / ``llm_map`` pair a SELECTION pass gets: loud refusals.

    The two-script contract forbids LLM work in the selection pass (the
    automatic scan must cost nothing), so its namespaces bind REFUSAL stubs —
    not the unwired-budget stubs — with a message naming the contract. A
    selection script that reaches for the LLM dies loudly instead of spending.
    """

    def llm_refused(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "llm_complete/llm_map are NOT available in a SELECTION script: the selection pass "
            "runs automatically and must cost nothing. Put LLM work in the APPLY script — "
            "there the Run flow approves a budget first."
        )

    return {"llm_complete": llm_refused, "llm_map": llm_refused}


def build_llm_script_helpers(
    runtime: LlmRuntimePort | None,
    ledger: LlmLedger | None,
    loop: Any,
    progress: Callable[[int], None] | None = None,
    batch_size: int = LLM_MAP_BATCH_SIZE,
    text_cache: TextCachePort | None = None,
) -> dict[str, Any]:
    """Build the sync ``llm_complete`` / ``llm_map`` helpers for one script pass.

    ``loop`` is the script's dedicated event loop (the same one the other sync
    helpers drive their async ports with). When either ``runtime`` or ``ledger``
    is None the helpers are loud-failure stubs: an LLM-calling script cannot run
    silently outside the approved-budget path.

    ``text_cache`` (when wired) is checked BEFORE the ledger reserves budget and
    before any request is issued: a cache hit costs no money and no request. The
    key includes the runtime's model identity, so a table refresh reuses the
    previous pass's answers for unchanged inputs but never crosses models.
    """
    if runtime is None or ledger is None:

        def llm_unwired(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(
                "llm helpers require a wired LlmRuntimePort and approved budget (got None). "
                "Run the pass through the review flow so a budget is approved first."
            )

        return {"llm_complete": llm_unwired, "llm_map": llm_unwired}

    effective_batch = max(1, batch_size)
    cache_namespace = runtime.cache_identity()

    def llm_complete(instruction: str, text: str) -> str:
        """Run ``instruction`` over one ``text``; return the model's reply string."""
        cache_key = llm_complete_cache_key(namespace=cache_namespace, instruction=instruction, text=text)
        if text_cache is not None:
            cached = text_cache.get_text(cache_key)
            if cached is not None:
                if progress is not None:
                    progress(1)
                return cached
        payload = f"{instruction}\n\n{text}"
        est_in = estimate_tokens(payload)
        est_out = est_in  # a reply of comparable length is the common shape
        ledger.reserve(est_in_tokens=est_in, est_out_tokens=est_out, requests=1)
        result = loop.run_until_complete(runtime.complete(instruction, text))
        ledger.record(result.usage)
        if text_cache is not None:
            text_cache.put_text(cache_key, result.output)
        if progress is not None:
            progress(1)
        return result.output

    def llm_map(instruction: str, items: list[str]) -> list[str]:
        """Run ``instruction`` over ``items`` in batches; return one output per item.

        Items are processed in batches of ``LLM_MAP_BATCH_SIZE`` (one request per
        batch). A batch whose reply cannot be parsed after the adapter's retry is
        logged and filled with empty strings (those entities simply produce no
        suggestion); the pass continues — the attempt's done/total counters
        expose the gap. The budget, by contrast, aborts the whole pass.
        """
        outputs: list[str] = []
        for start in range(0, len(items), effective_batch):
            batch = items[start : start + effective_batch]
            cache_key = llm_map_cache_key(namespace=cache_namespace, instruction=instruction, items=batch)
            if text_cache is not None:
                cached = text_cache.get_text(cache_key)
                if cached is not None:
                    # A batch is a JSON array in the cache: keep the exact list
                    # shape so a hit and a live call are indistinguishable.
                    try:
                        parsed = json.loads(cached)
                    except json.JSONDecodeError:
                        parsed = None
                    if (
                        isinstance(parsed, list)
                        and len(parsed) == len(batch)
                        and all(isinstance(item, str) for item in parsed)
                    ):
                        outputs.extend(parsed)
                        if progress is not None:
                            progress(len(batch))
                        continue
            est_in = estimate_tokens(instruction) + sum(estimate_tokens(item) for item in batch)
            est_out = sum(estimate_tokens(item) for item in batch)
            ledger.reserve(est_in_tokens=est_in, est_out_tokens=est_out, requests=1)
            try:
                result = loop.run_until_complete(runtime.map_batch(instruction, batch))
                outputs.extend(result.outputs)
                ledger.record(result.usage)
                if text_cache is not None:
                    text_cache.put_text(cache_key, json.dumps(result.outputs, ensure_ascii=False))
            except LlmBatchFormatError as exc:
                logger.warning("llm_map batch {}-{} unparseable after retry: {}", start, start + len(batch), exc)
                outputs.extend([""] * len(batch))
                # A format failure still cost a request (possibly two); charge the
                # estimate so the budget cannot be silently overrun by a model
                # that keeps replying with prose.
                ledger.record(LlmUsage(requests=1, input_tokens=est_in, output_tokens=est_out))
            if progress is not None:
                progress(len(batch))
        return outputs

    return {"llm_complete": llm_complete, "llm_map": llm_map}


__all__ = ["build_llm_script_helpers", "selection_llm_refusal"]
