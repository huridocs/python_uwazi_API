from uwazi_api.drivers.http_client import HttpClient
from uwazi_api.drivers.repositories.entity_repository import EntityRepository
from uwazi_api.drivers.repositories.template_repository import TemplateRepository
from uwazi_api.drivers.repositories.file_repository import FileRepository
from uwazi_api.drivers.repositories.csv_repository import CSVRepository
from uwazi_api.drivers.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.drivers.repositories.relationship_repository import RelationshipRepository
from uwazi_api.drivers.repositories.settings_repository import SettingsRepository
from uwazi_api.drivers.repositories.search_repository import SearchRepository
from uwazi_api.use_cases.file_service import FileService
from uwazi_api.use_cases.csv_import import CSVImportUseCase
from uwazi_api.use_cases.entity_export import EntityExportUseCase


class UwaziClient:
    def __init__(self, user: str, password: str, url: str):
        self.http = HttpClient(url, user, password)

        # Drivers / Repositories
        self._entity_repo = EntityRepository(self.http)
        self._template_repo = TemplateRepository(self.http)
        self._file_repo = FileRepository(self.http)
        self._csv_repo = CSVRepository(self.http)
        self._thesauri_repo = ThesauriRepository(self.http)
        self._relationship_repo = RelationshipRepository(self.http)
        self._settings_repo = SettingsRepository(self.http)
        self._search_repo = SearchRepository(self.http)

        # Use cases / services
        self._file_service = FileService(self._file_repo, self._entity_repo)
        self._csv_import = CSVImportUseCase(self._csv_repo, self._template_repo, self._entity_repo)
        self._entity_export = EntityExportUseCase(self._entity_repo, self._template_repo)

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
    def csv(self) -> CSVImportUseCase:
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
