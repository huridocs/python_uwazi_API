"""Prediction cache boundary: label predictions from sources, keyed per PDF."""

from abc import ABC, abstractmethod

from uwazi_property_filler.domain.label import LabelPrediction


class PredictionStorePort(ABC):
    @abstractmethod
    async def save_predictions(self, instance_key: str, shared_id: str, predictions: list[LabelPrediction]) -> None: ...

    @abstractmethod
    async def list_predictions(self, instance_key: str, shared_id: str) -> list[LabelPrediction]: ...

    @abstractmethod
    async def clear_predictions(self, instance_key: str, shared_id: str) -> None: ...
