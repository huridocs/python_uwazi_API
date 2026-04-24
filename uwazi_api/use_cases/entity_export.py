from typing import Optional

import pandas as pd

from uwazi_api.use_cases.repositories.entity_repository import EntityRepository
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.entity_to_dataframe import entities_to_dataframe


class EntityExportUseCase:
    def __init__(
        self,
        entity_repository: "EntityRepository",
        template_repository: "TemplateRepository",
    ):
        self.entity_repo = entity_repository
        self.template_repo = template_repository

    def to_dataframe(
        self,
        start_from: int = 0,
        batch_size: int = 30,
        template_name: Optional[str] = None,
        language: str = "en",
        published: Optional[bool] = None,
    ) -> pd.DataFrame:
        entities = self.entity_repo.get(start_from, batch_size, template_name, language, published)
        return entities_to_dataframe(entities, template_name, self.template_repo)
