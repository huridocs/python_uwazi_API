from typing import Optional

from uwazi_agent.adapters.template_mapper import TemplateMapperAdapter, ThesaurusGateway
from uwazi_api.client import UwaziClient


class UwaziThesaurusGateway(ThesaurusGateway):
    def __init__(self, client: UwaziClient, language: str = "en"):
        self._repo = client.thesauris
        self._language = language

    def id_for_name(self, name: str) -> Optional[str]:
        for t in self._repo.get(self._language):
            if t.name == name:
                return t.id
        return None

    def name_for_id(self, thesaurus_id: str) -> Optional[str]:
        for t in self._repo.get(self._language):
            if t.id == thesaurus_id:
                return t.name
        return None


def build_template_mapper_from_client(client: UwaziClient) -> TemplateMapperAdapter:
    return TemplateMapperAdapter(thesaurus_gateway=UwaziThesaurusGateway(client))
