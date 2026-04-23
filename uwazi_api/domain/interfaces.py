from abc import ABC, abstractmethod
from typing import List, Optional

from uwazi_api.domain.models import Entity, Template, Thesauri, Settings, Reference


class _CacheClearMixin:
    def clear_cache(self, *args, **kwargs):
        """Default no-op cache clear. Override in subclasses that implement caching."""
        pass


class EntityRepositoryInterface(ABC):
    @abstractmethod
    def get_one(self, shared_id: str, language: str) -> Entity: ...

    @abstractmethod
    def get_id(self, shared_id: str, language: str) -> str: ...

    @abstractmethod
    def get_shared_ids(self, to_process_template: str, batch_size: int, unpublished: bool = True) -> List[str]: ...

    @abstractmethod
    def get(
        self,
        start_from: int = 0,
        batch_size: int = 30,
        template_id: Optional[str] = None,
        language: str = "en",
        published: Optional[bool] = None,
    ) -> List[Entity]: ...

    @abstractmethod
    def get_by_id(self, entity_id: str) -> Optional[Entity]: ...

    @abstractmethod
    def upload(self, entity: dict, language: str) -> str: ...

    @abstractmethod
    def delete(self, shared_id: str) -> None: ...

    @abstractmethod
    def publish_entities(self, shared_ids: List[str]) -> None: ...

    @abstractmethod
    def delete_entities(self, shared_ids: List[str]) -> None: ...

    @abstractmethod
    def search_by_text(
        self,
        search_term: str,
        template_id: Optional[str] = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
    ) -> List[Entity]: ...


class TemplateRepositoryInterface(ABC, _CacheClearMixin):
    @abstractmethod
    def get(self) -> List[Template]: ...

    @abstractmethod
    def set(self, language: str, template: dict) -> dict: ...

    @abstractmethod
    def get_by_name(self, template_name: str) -> Optional[Template]: ...

    @abstractmethod
    def get_by_id(self, template_id: str) -> Optional[Template]: ...


class FileRepositoryInterface(ABC):
    @abstractmethod
    def get_document_by_file_name(self, file_name: str) -> Optional[bytes]: ...

    @abstractmethod
    def upload_file(self, pdf_file_path: str, share_id: str, language: str, title: str) -> bool: ...

    @abstractmethod
    def upload_document_from_bytes(
        self, file_bytes: bytes, share_id: str, language: str, title: str, file_type: str
    ) -> bool: ...

    @abstractmethod
    def upload_file_from_bytes(
        self, file_bytes: bytes, share_id: str, language: str, title: str, file_type: str
    ) -> bool: ...

    @abstractmethod
    def upload_image(self, image_binary: bytes, title: str, entity_shared_id: str, language: str) -> Optional[dict]: ...

    @abstractmethod
    def delete_file(self, file_id: str) -> bool: ...


class CSVRepositoryInterface(ABC):
    @abstractmethod
    def upload(self, template_id: str, csv_bytes: bytes, filename: str = "import.csv") -> Optional[dict]: ...


class ThesauriRepositoryInterface(ABC, _CacheClearMixin):
    @abstractmethod
    def get(self, language: str) -> List[Thesauri]: ...

    @abstractmethod
    def add_value(self, thesauri_name: str, thesauri_id: str, thesauri_values: dict, language: str) -> dict: ...


class RelationshipRepositoryInterface(ABC):
    @abstractmethod
    def create(
        self,
        file_entity_shared_id: str,
        file_id: str,
        reference: Reference,
        to_entity_shared_id: str,
        relationship_type_id: str,
        language: str = "en",
    ) -> Optional[dict]: ...


class SettingsRepositoryInterface(ABC):
    @abstractmethod
    def get(self) -> Settings: ...

    @abstractmethod
    def get_languages(self) -> List[str]: ...
