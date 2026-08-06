from abc import ABC, abstractmethod

from uwazi_api.domain.stats import SearchStats


class StatsApiPort(ABC):
    @abstractmethod
    async def get_stats(self, language: str = "en") -> SearchStats: ...
