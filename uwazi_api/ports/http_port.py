from abc import ABC, abstractmethod
from typing import Optional

from requests import Session


class HttpClientPort(ABC):
    url: str
    headers: dict
    connect_sid: str
    graylog: object
    request_adapter: Session

    @abstractmethod
    def _get_connect_sid(self) -> str:
        pass
