from abc import ABC, abstractmethod


class PdfCachePort(ABC):
    @abstractmethod
    async def get(self, filename: str) -> bytes | None: ...

    @abstractmethod
    async def put(self, filename: str, data: bytes) -> None: ...

    @abstractmethod
    async def clear(self) -> None: ...
