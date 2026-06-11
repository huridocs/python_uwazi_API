from typing import Optional

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.use_cases.repositories.entity_repository import EntityRepository
from uwazi_api.use_cases.repositories.entity_validator import EntityValidator
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.file_repository import FileRepository
from uwazi_api.use_cases.repositories.csv_repository import CSVRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.use_cases.repositories.relationship_repository import RelationshipRepository
from uwazi_api.use_cases.repositories.settings_repository import SettingsRepository
from uwazi_api.use_cases.repositories.search_repository import SearchRepository
from uwazi_api.use_cases.repositories.pages_repository import PagesRepository
from uwazi_api.use_cases.file_service import FileService
from uwazi_api.use_cases.csv_use_case import CSVUseCase
from uwazi_api.use_cases.entity_export import EntityExportUseCase
from uwazi_api.use_cases.thesauri_from_dataframe_use_case import ThesauriFromDataframeUseCase


class UwaziClient:
    def __init__(self, user: Optional[str] = None, password: Optional[str] = None, url: Optional[str] = None):
        self.http = HttpClientAdapter(url, user, password)

        # Drivers / Repositories
        self._template_repo = TemplateRepository(self.http)
        self._file_repo = FileRepository(self.http)
        self._csv_repo = CSVRepository(self.http)
        self._thesauri_repo = ThesauriRepository(self.http)
        self._validator = EntityValidator(template_repo=self._template_repo, thesauri_repo=self._thesauri_repo)
        self._entity_repo = EntityRepository(
            self.http, template_repo=self._template_repo, thesauri_repo=self._thesauri_repo, validator=self._validator
        )
        self._relationship_repo = RelationshipRepository(self.http)
        self._settings_repo = SettingsRepository(self.http)
        self._search_repo = SearchRepository(self.http, self._template_repo, self._thesauri_repo)
        self._pages_repo = PagesRepository(self.http)

        # Use cases / services
        self._file_service = FileService(self._file_repo, self._entity_repo)
        self._csv_import = CSVUseCase(self._csv_repo, self._template_repo, self._entity_repo)
        self._entity_export = EntityExportUseCase(self._entity_repo, self._template_repo, self._search_repo)
        self._thesauri_from_df = ThesauriFromDataframeUseCase(self._template_repo, self._thesauri_repo)

    @classmethod
    def public(cls, url: str) -> "UwaziClient":
        return cls(url=url)

    @property
    def entities(self) -> "EntityRepository":
        return self._entity_repo

    @property
    def templates(self) -> "TemplateRepository":
        return self._template_repo

    @property
    def files(self) -> FileService:
        return self._file_service

    @property
    def csv(self) -> CSVUseCase:
        return self._csv_import

    @property
    def thesauris(self) -> "ThesauriRepository":
        return self._thesauri_repo

    @property
    def relationships(self) -> "RelationshipRepository":
        return self._relationship_repo

    @property
    def settings(self) -> "SettingsRepository":
        return self._settings_repo

    @property
    def search(self) -> SearchRepository:
        return self._search_repo

    @property
    def exports(self) -> EntityExportUseCase:
        return self._entity_export

    @property
    def thesauri_from_df(self) -> ThesauriFromDataframeUseCase:
        return self._thesauri_from_df

    @property
    def pages(self) -> PagesRepository:
        return self._pages_repo
