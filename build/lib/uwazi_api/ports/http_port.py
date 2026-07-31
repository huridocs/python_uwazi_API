from abc import ABC
from typing import Optional

from requests import Session


class HttpClientPort(ABC):
    url: str
    headers: dict
    connect_sid: Optional[str]
    request_adapter: Session
