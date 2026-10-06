"""Outbound notification boundary: inform external services of label changes."""

from abc import ABC, abstractmethod

from uwazi_property_filler.domain.label import Label


class LabelNotifyPort(ABC):
    @abstractmethod
    async def notify_created(self, label: Label) -> None: ...

    @abstractmethod
    async def notify_deleted(self, label: Label) -> None: ...
