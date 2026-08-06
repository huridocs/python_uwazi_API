from abc import ABC, abstractmethod

from uwazi_api.domain.language import Language
from uwazi_api.domain.menu_link import MenuLink


class SettingsApiPort(ABC):
    @abstractmethod
    async def get_languages(self) -> list[Language]: ...

    @abstractmethod
    async def get_menu_links(self) -> list[MenuLink]: ...

    @abstractmethod
    async def set_menu_links(self, links: list[MenuLink]) -> list[MenuLink]: ...

    @abstractmethod
    async def delete_all_menu_links(self) -> list[MenuLink]: ...
