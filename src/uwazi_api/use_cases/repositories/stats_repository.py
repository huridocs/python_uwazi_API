import json
from typing import Optional

from uwazi_api.domain.exceptions import SearchError
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.stats import SearchStats, TemplateStat, ThesaurusValueStat
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter


class StatsRepository:
    def __init__(
        self,
        http_client: HttpClientAdapter,
        template_repo: Optional[TemplateRepository] = None,
        thesauri_repo: Optional[ThesauriRepository] = None,
    ):
        self.http = http_client
        self._template_repo = template_repo
        self._thesauri_repo = thesauri_repo

    def get_stats(self, language: str = "en") -> SearchStats:
        params = {
            "allAggregations": "true",
            "from": "0",
            "includeUnpublished": "true",
            "limit": "30",
            "order": "desc",
            "sort": "creationDate",
            "treatAs": "number",
            "unpublished": "false",
            "aggregatePublishingStatus": "true",
            "aggregatePermissionsByUsers": "true",
            "include": '["permissions"]',
        }

        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"locale": language},
        )
        if response.status_code != 200:
            raise SearchError(f"Error fetching search stats: {response.status_code}")

        data = json.loads(response.content)

        total_rows = data.get("totalRows", 0)
        aggregations = data.get("aggregations", {})
        rows = data.get("rows", [])

        template_stats = self._build_template_stats(aggregations, rows)
        thesaurus_stats = self._build_thesaurus_stats(rows, language)

        total_from_templates = sum(ts.count for ts in template_stats)
        total_entities = max(total_rows, total_from_templates)

        return SearchStats(
            total_entities=total_entities,
            templates=template_stats,
            thesauri=thesaurus_stats,
        )

    def _build_template_stats(self, aggregations: dict, rows: list[dict]) -> list[TemplateStat]:
        counts: dict[str, int] = {}

        types_buckets: list[dict] = []
        if aggregations:
            types_buckets = aggregations.get("_types", {}).get("buckets", []) or []
            if not types_buckets:
                nested_all = aggregations.get("all", {}) or {}
                nested_types = nested_all.get("_types", {}) or {}
                types_buckets = nested_types.get("buckets", []) or []

        for bucket in types_buckets:
            key = bucket.get("key")
            if not key:
                continue
            counts[key] = bucket.get("doc_count", 0)

        for row in rows or []:
            tmpl_id = row.get("template")
            if not tmpl_id or tmpl_id in counts:
                continue
            counts[tmpl_id] = counts.get(tmpl_id, 0) + 1

        stats: list[TemplateStat] = []
        for template_id, count in sorted(counts.items(), key=lambda x: -x[1]):
            template_name = template_id
            if self._template_repo:
                template = self._template_repo.get_by_id(template_id)
                if template:
                    template_name = template.name
            stats.append(TemplateStat(template_id=template_id, template_name=template_name, count=count))

        return stats

    def _build_thesaurus_stats(self, rows: list[dict], language: str) -> list[ThesaurusValueStat]:
        if not self._template_repo or not self._thesauri_repo:
            return []

        templates = self._template_repo.get()
        thesauri_list = self._thesauri_repo.get(language=language)

        prop_name_to_thesauri = {}
        for tmpl in templates:
            for prop in tmpl.properties:
                if prop.type in (PropertyType.SELECT, PropertyType.MULTI_SELECT) and prop.content:
                    thesaurus = next((t for t in thesauri_list if t.id == prop.content), None)
                    if thesaurus:
                        prop_name_to_thesauri[prop.name] = thesaurus

        value_counts: dict[tuple[str, str], int] = {}
        for row in rows:
            metadata = row.get("metadata", {})
            for prop_name, values in metadata.items():
                thesaurus = prop_name_to_thesauri.get(prop_name)
                if not thesaurus or not isinstance(values, list):
                    continue
                for val in values:
                    if isinstance(val, dict):
                        v_id = val.get("value")
                        if v_id:
                            key = (thesaurus.id, v_id)
                            value_counts[key] = value_counts.get(key, 0) + 1

        stats: list[ThesaurusValueStat] = []
        value_id_to_label: dict[str, str] = {}
        for t in thesauri_list:
            for v in t.values:
                value_id_to_label[v.id] = v.label
                if v.values:
                    for child in v.values:
                        value_id_to_label[child.id] = child.label

        for (thes_id, val_id), count in sorted(value_counts.items(), key=lambda x: -x[1]):
            thesaurus = next((t for t in thesauri_list if t.id == thes_id), None)
            thes_name = thesaurus.name if thesaurus else thes_id
            val_label = value_id_to_label.get(val_id, val_id)
            stats.append(
                ThesaurusValueStat(
                    thesaurus_id=thes_id,
                    thesaurus_name=thes_name,
                    value_id=val_id,
                    value_label=val_label,
                    count=count,
                )
            )

        return stats
