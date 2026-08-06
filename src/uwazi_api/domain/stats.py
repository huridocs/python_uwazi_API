from pydantic import BaseModel


class TemplateStat(BaseModel):
    template_id: str
    template_name: str
    count: int


class ThesaurusValueStat(BaseModel):
    thesaurus_id: str
    thesaurus_name: str
    value_id: str
    value_label: str
    count: int


class SearchStats(BaseModel):
    total_entities: int
    templates: list[TemplateStat]
    thesauri: list[ThesaurusValueStat]
