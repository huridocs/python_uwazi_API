from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DomainError(Exception):
    """Base domain exception."""

    pass


class EntityNotFoundError(DomainError):
    pass


class UploadError(DomainError):
    pass


class SearchError(DomainError):
    pass


class TemplateNotFoundError(DomainError):
    pass


class AuthenticationError(DomainError):
    pass
