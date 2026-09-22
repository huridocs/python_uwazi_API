from abc import ABC, abstractmethod
from typing import Any

from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.domain.extension_request import ExtensionContext
from uwazi_property_filler.domain.highlight import Highlight
from uwazi_property_filler.domain.suggestion import Suggestion


class ExtensionPort(ABC):
    """The single pluggable interface. Every method returns an empty result
    when its category is unsupported by the concrete extension.
    """

    record: ExtensionRecord

    @abstractmethod
    async def suggest(self, ctx: ExtensionContext) -> list[Suggestion]: ...

    @abstractmethod
    async def highlight(self, ctx: ExtensionContext) -> list[Highlight]: ...

    @abstractmethod
    async def display(self, ctx: ExtensionContext) -> str | None: ...

    @abstractmethod
    async def search(self, ctx: ExtensionContext) -> list[dict[str, str]]: ...

    @abstractmethod
    async def fill(self, ctx: ExtensionContext) -> dict[str, Any] | None: ...
