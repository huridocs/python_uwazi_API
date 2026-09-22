from abc import ABC, abstractmethod

from uwazi_property_filler.domain.extension_category import ExtensionCategory
from uwazi_property_filler.domain.extension_record import ExtensionRecord


class ExtensionRegistryPort(ABC):
    @abstractmethod
    async def list_extensions(self) -> list[ExtensionRecord]: ...

    @abstractmethod
    async def get_enabled(self, category: ExtensionCategory) -> list[ExtensionRecord]: ...

    @abstractmethod
    async def save_extension(self, record: ExtensionRecord) -> None: ...

    @abstractmethod
    async def set_enabled(self, extension_id: str, enabled: bool) -> None: ...
