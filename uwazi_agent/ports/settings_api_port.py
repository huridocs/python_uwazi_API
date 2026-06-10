from abc import ABC, abstractmethod

from uwazi_api.domain.language import Language


class SettingsApiPort(ABC):
    @abstractmethod
    async def get_languages(self) -> list[Language]: ...
