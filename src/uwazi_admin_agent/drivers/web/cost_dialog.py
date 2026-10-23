"""One reusable "approve this LLM spend" dialog for every LLM-spending action.

Three surfaces spend tokens: the generation wizard, a prediction pass, and the
prompt-improvement (repair) turn. All three must show the operator what a pass
will cost and let them tighten the HARD caps before approving — which is why
this is one component with a caller-supplied estimate rather than three
one-off dialogs.

What the dialog is careful to be honest about:

- money is labelled an ESTIMATE (the prices are operator-configured
  placeholders and the call count is a projection);
- the request/token ceilings ARE the enforced limits — the note says so — and
  the operator can lower them here;
- when pricing is unconfigured, request/token counts still render and the money
  line says so instead of inventing a figure.
"""

from __future__ import annotations

from typing import Any, Callable

from nicegui import context, ui
from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.configuration import (
    LLM_PRICE_INPUT_PER_MTOK,
    LLM_PRICE_OUTPUT_PER_MTOK,
    PREDICTION_DEFAULT_MAX_COST,
    PREDICTION_DEFAULT_MAX_REQUESTS,
)
from uwazi_admin_agent.domain.llm_budget import (
    LlmBudget,
    LlmPrice,
    estimate_pass_cost,
    format_money,
)


class SpendEstimate(BaseModel):
    """Everything the dialog needs to describe a pass before it runs."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(description="Dialog title, e.g. 'Generate outcomes'.")
    detail: str = Field(default="", description="One line explaining what is about to run.")
    n_calls: int = Field(ge=0, description="Projected LLM requests for the pass.")
    n_entities: int = Field(default=0, ge=0, description="How many entities the pass covers (0 when unknown).")
    est_in_tokens_per_call: int = Field(default=0, ge=0, description="Estimated input tokens per request.")
    est_out_tokens_per_call: int = Field(default=0, ge=0, description="Estimated output tokens per request.")
    default_max_cost: float = Field(default=PREDICTION_DEFAULT_MAX_COST, gt=0)
    default_max_requests: int = Field(default=PREDICTION_DEFAULT_MAX_REQUESTS, gt=0)
    notes: list[str] = Field(default_factory=list, description="Caveats shown verbatim in the dialog.")
    entity_scope: list[str] | None = Field(
        default=None,
        description="The subset this pass covers (None = the whole instance), passed through to the caller.",
    )

    def price(self) -> LlmPrice:
        return LlmPrice(
            model=_model_name(),
            input_per_mtok=LLM_PRICE_INPUT_PER_MTOK,
            output_per_mtok=LLM_PRICE_OUTPUT_PER_MTOK,
        )

    def est_cost(self) -> float:
        return estimate_pass_cost(self.price(), self.n_calls, self.est_in_tokens_per_call, self.est_out_tokens_per_call)


def _model_name() -> str:
    """The configured model name (falls back to a placeholder when unreadable)."""
    try:
        from uwazi_agent.configuration import MODEL

        return MODEL
    except Exception:  # noqa: BLE001 — a missing/renamed constant must not break the dialog
        return "(model not configured)"


def _estimate_rows(estimate: SpendEstimate) -> list[tuple[str, str]]:
    """The estimate's label/value rows (pure, so the numbers are reviewable in a test)."""
    rows: list[tuple[str, str]] = [
        ("Entities in this pass", f"{estimate.n_entities:,}" if estimate.n_entities else "unknown (whole instance)"),
        ("LLM requests (projected)", f"{estimate.n_calls:,}"),
        ("Input tokens (est. per request)", f"{estimate.est_in_tokens_per_call:,}"),
        ("Output tokens (est. per request)", f"{estimate.est_out_tokens_per_call:,}"),
    ]
    price = estimate.price()
    if price.configured():
        rows.append(("Estimated cost", f"{format_money(estimate.est_cost())} (projection)"))
    else:
        rows.append(("Estimated cost", "pricing unconfigured — cannot project or enforce a dollar cap"))
    return rows


def estimate_summary(estimate: SpendEstimate) -> str:
    """A one-line textual summary (used in logs and by tests)."""
    return (
        f"{estimate.title}: {estimate.n_entities:,} entities, {estimate.n_calls:,} projected requests, "
        f"estimated cost {format_money(estimate.est_cost())}"
    )


def build_budget(
    estimate: SpendEstimate,
    max_cost: float | None = None,
    max_requests: int | None = None,
) -> LlmBudget:
    """Assemble the approved budget from the dialog's (possibly tightened) caps (pure).

    The dollar cap is the enforced limit (checked against price-derived cost);
    the request count is the backstop against cheap-call loops.
    """
    return LlmBudget(
        max_cost=max_cost if max_cost and max_cost > 0 else estimate.default_max_cost,
        max_requests=max_requests if max_requests and max_requests > 0 else estimate.default_max_requests,
        price=estimate.price(),
    )


def confirm_llm_spend(
    estimate: SpendEstimate,
    on_approve: Callable[[LlmBudget], Any],
    on_cancel: Callable[[], Any] | None = None,
) -> None:
    """Open the approval dialog; call ``on_approve(budget)`` only when approved.

    The dollar cap (the enforced limit) and the request backstop are editable
    inputs prefilled from the estimate's defaults, so the operator can lower
    them after seeing the projected call count — and never raise them by
    accident (an empty/invalid input falls back to the default).

    ``on_cancel`` is called when the operator closes the dialog WITHOUT
    approving — the caller may need to undo what it staged before the dialog
    opened (e.g. a pending message in a chat).
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
            ui.label(estimate.title).classes("text-h6")
            with ui.column().classes("w-full gap-0"):
                for label, value in _estimate_rows(estimate):
                    with ui.row().classes("w-full items-center justify-between no-wrap"):
                        ui.label(label).classes("text-body2 text-grey-7")
                        ui.label(value).classes("text-body2 text-bold")
            if estimate.detail:
                ui.label(estimate.detail).classes("text-body2 q-mt-sm")
            ui.separator()
            ui.label("Hard limits enforced at runtime (the projection above is only an estimate):").classes(
                "text-body2 text-bold"
            )
            with ui.row().classes("w-full items-center gap-2"):
                cost_input = ui.number(
                    "Max cost ($)", value=estimate.default_max_cost, min=0.01, step=0.5, format="%.2f"
                ).classes("grow")
                requests_input = ui.number(
                    "Max requests (backstop)", value=estimate.default_max_requests, min=1, step=1
                ).classes("grow")
            for note in estimate.notes:
                ui.label(note).classes("text-body2 text-grey-7")
            ui.label(
                "The pass aborts cleanly the moment the estimated cost of the calls so far would pass the dollar "
                "cap. Predictions never write to Uwazi, so an aborted pass keeps everything it already recorded."
            ).classes("text-body2 text-grey-7")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: _cancel(dialog, on_cancel)).props("color=grey-7 flat")
                ui.button(
                    "Approve and run",
                    on_click=lambda: _approve(dialog, estimate, on_approve, cost_input, requests_input),
                )
    dialog.open()


def _cancel(dialog: Any, on_cancel: Callable[[], Any] | None) -> None:
    """Close the dialog and tell the caller the spend was NOT approved."""
    dialog.close()
    if on_cancel is not None:
        on_cancel()


def _approve(
    dialog: Any,
    estimate: SpendEstimate,
    on_approve: Callable[[LlmBudget], Any],
    cost_input: Any,
    requests_input: Any,
) -> None:
    """Close the dialog and hand the approved budget to the caller."""
    budget = build_budget(
        estimate,
        max_cost=_as_float(cost_input.value),
        max_requests=_as_int(requests_input.value),
    )
    dialog.close()
    on_approve(budget)


def _as_float(value: Any) -> float | None:
    """Coerce an input value to a positive float (None/invalid → None = 'keep the default')."""
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed > 0 else None


def _as_int(value: Any) -> int | None:
    """Coerce an input value to an int (None/invalid → None, which means 'keep the default')."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["SpendEstimate", "build_budget", "confirm_llm_spend", "estimate_summary"]
