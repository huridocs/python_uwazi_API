from typing import Optional

from uwazi_agent.adapters.template_mapper import (
    RelationTypeGateway,
    TemplateGateway,
    TemplateMapperAdapter,
    ThesaurusGateway,
)
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


class UwaziTemplateGateway(TemplateGateway):
    def __init__(self, client: UwaziClient):
        self._repo = client.templates

    def id_for_name(self, name: str) -> Optional[str]:
        template = self._repo.get_by_name(name)
        return template.id if template else None

    def name_for_id(self, template_id: str) -> Optional[str]:
        template = self._repo.get_by_id(template_id)
        return template.name if template else None


class UwaziRelationTypeGateway(RelationTypeGateway):
    def __init__(self, client: UwaziClient):
        self._repo = client.relationships

    def id_for_name(self, name: str) -> Optional[str]:
        return self._repo.resolve_relation_type_id(name)

    def name_for_id(self, relation_type_id: str) -> Optional[str]:
        relation_type = self._repo.get_relation_type_by_id(relation_type_id)
        return relation_type.name if relation_type else None


def build_template_mapper_from_client(client: UwaziClient) -> TemplateMapperAdapter:
    return TemplateMapperAdapter(
        thesaurus_gateway=UwaziThesaurusGateway(client),
        template_gateway=UwaziTemplateGateway(client),
        relation_type_gateway=UwaziRelationTypeGateway(client),
    )
