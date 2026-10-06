"""Gather + cache label predictions from enabled sources (extensions)."""

from __future__ import annotations

from uwazi_property_filler.domain.extension_request import ExtensionContext
from uwazi_property_filler.domain.label import LabelPrediction
from uwazi_property_filler.ports.extension_port import ExtensionPort
from uwazi_property_filler.ports.prediction_store_port import PredictionStorePort


def dedupe_predictions(predictions: list[LabelPrediction]) -> list[LabelPrediction]:
    """Drop identical predictions (same source, label target, and text)."""
    seen: set[tuple[str, str, str]] = set()
    result: list[LabelPrediction] = []
    for p in predictions:
        key = (p.source, p.label_shared_id, p.text)
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


async def get_predictions(
    ctx: ExtensionContext,
    labelers: list[ExtensionPort],
    store: PredictionStorePort,
    instance_key: str,
) -> list[LabelPrediction]:
    """Call every enabled labeler, dedupe, replace the cache, and return."""
    predictions: list[LabelPrediction] = []
    for runner in labelers:
        predictions.extend(await runner.label(ctx))
    predictions = dedupe_predictions(predictions)
    await store.clear_predictions(instance_key, ctx.shared_id)
    await store.save_predictions(instance_key, ctx.shared_id, predictions)
    return predictions
