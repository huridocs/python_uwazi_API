from uwazi_api.domain.interfaces import (
    EntityRepositoryInterface,
    TemplateRepositoryInterface,
    FileRepositoryInterface,
    CSVRepositoryInterface,
    ThesauriRepositoryInterface,
    RelationshipRepositoryInterface,
    SettingsRepositoryInterface,
)
from uwazi_api.drivers.http_client import HttpClient
from uwazi_api.drivers.repositories.entity_repository import EntityRepository
from uwazi_api.drivers.repositories.template_repository import TemplateRepository
from uwazi_api.drivers.repositories.file_repository import FileRepository
from uwazi_api.drivers.repositories.csv_repository import CSVRepository
from uwazi_api.drivers.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.drivers.repositories.relationship_repository import RelationshipRepository
from uwazi_api.drivers.repositories.settings_repository import SettingsRepository
from uwazi_api.use_cases.file_service import FileService
from uwazi_api.use_cases.csv_import import CSVImportUseCase
from uwazi_api.use_cases.entity_export import EntityExportUseCase


class UwaziClient:
    def __init__(self, user: str, password: str, url: str):
        self.http = HttpClient(url, user, password)

        # Drivers / Repositories
        self._entity_repo: EntityRepositoryInterface = EntityRepository(self.http)
        self._template_repo: TemplateRepositoryInterface = TemplateRepository(self.http)
        self._file_repo: FileRepositoryInterface = FileRepository(self.http)
        self._csv_repo: CSVRepositoryInterface = CSVRepository(self.http)
        self._thesauri_repo: ThesauriRepositoryInterface = ThesauriRepository(self.http)
        self._relationship_repo: RelationshipRepositoryInterface = RelationshipRepository(self.http)
        self._settings_repo: SettingsRepositoryInterface = SettingsRepository(self.http)

        # Use cases / services
        self._file_service = FileService(self._file_repo, self._entity_repo)
        self._csv_import = CSVImportUseCase(self._csv_repo, self._template_repo, self._entity_repo)
        self._entity_export = EntityExportUseCase(self._entity_repo, self._template_repo)

    @property
    def entities(self) -> EntityRepositoryInterface:
        return self._entity_repo

    @property
    def templates(self) -> TemplateRepositoryInterface:
        return self._template_repo

    @property
    def files(self) -> FileService:
        return self._file_service

    @property
    def csv(self) -> CSVImportUseCase:
        return self._csv_import

    @property
    def thesauris(self) -> ThesauriRepositoryInterface:
        return self._thesauri_repo

    @property
    def relationships(self) -> RelationshipRepositoryInterface:
        return self._relationship_repo

    @property
    def settings(self) -> SettingsRepositoryInterface:
        return self._settings_repo

    @property
    def exports(self) -> EntityExportUseCase:
        return self._entity_export
