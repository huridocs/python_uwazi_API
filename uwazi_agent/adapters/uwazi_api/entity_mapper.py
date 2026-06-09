from datetime import date, datetime
from datetime import timezone
from typing import Any, Optional

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import SearchError
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.use_cases.sanitize_property_label import PropertyLabelSanitizer

from uwazi_agent.domain.agent_entity import AgentEntity


_SIMPLE_PROPS: set[PropertyType] = {
    PropertyType.TEXT,
    PropertyType.MARKDOWN,
    PropertyType.GENERATED_ID,
    PropertyType.IMAGE,
    PropertyType.MEDIA,
}


class EntityMapper:
    """Convert between API-side ``Entity`` (Uwazi) and ``AgentEntity`` (LLM-facing).

    - On the way out (``to_agent``) we drop heavy file descriptors and resolve
      thesaurus UUIDs back to labels so the LLM can reason in human terms.
    - On the way in (``to_api``) we coerce each metadata value into the
      ``[{"value": ...}]`` envelope Uwazi expects, using the template's
      property types to drive coercion. Thesauri values are always passed
      as labels and resolved to UUIDs here.
    """

    def __init__(
        self,
        template_repo: TemplateRepository,
        thesauri_repo: ThesauriRepository,
    ):
        self._template_repo = template_repo
        self._thesauri_repo = thesauri_repo

    def to_agent(
        self,
        api_entity: Entity,
        template_name: Optional[str] = None,
        language: str = "en",
    ) -> AgentEntity:
        if not api_entity.shared_id:
            raise SearchError(f"Entity is missing shared_id: {api_entity.model_dump(by_alias=True)}")

        resolved_template_name = template_name
        if not resolved_template_name and api_entity.template:
            tpl = self._template_repo.get_by_id(api_entity.template) or self._template_repo.get_by_name(api_entity.template)
            if tpl:
                resolved_template_name = tpl.name

        metadata: dict[str, Any] = {}
        if api_entity.metadata and resolved_template_name:
            template = self._template_repo.get_by_name(resolved_template_name) or self._template_repo.get_by_id(
                resolved_template_name
            )
            if template:
                metadata = self._extract_agent_metadata(api_entity.metadata, template, language)

        return AgentEntity(
            shared_id=api_entity.shared_id,
            title=api_entity.title or "",
            template_name=resolved_template_name or "",
            metadata=metadata,
            language=api_entity.language or "en",
            published=api_entity.published,
        )

    def to_api(
        self,
        agent_entity: AgentEntity,
        language: str = "en",
    ) -> Entity:
        if not agent_entity.template_name:
            raise SearchError(f"Entity {agent_entity.shared_id}: `template_name` is required so metadata can be coerced.")
        template = self._template_repo.get_by_name(agent_entity.template_name) or self._template_repo.get_by_id(
            agent_entity.template_name
        )
        if not template:
            raise SearchError(f"Template '{agent_entity.template_name}' not found")

        template_id = template.id or agent_entity.template_name
        coerced_metadata: dict[str, Any] = {}
        if agent_entity.metadata:
            coerced_metadata = self._coerce_metadata(agent_entity.metadata, template, language)

        return Entity(
            sharedId=agent_entity.shared_id,
            title=agent_entity.title,
            template=template_id,
            language=agent_entity.language or language,
            published=agent_entity.published,
            metadata=coerced_metadata,
        )

    def _extract_agent_metadata(
        self,
        api_metadata: dict[str, Any],
        template: Template,
        language: str,
    ) -> dict[str, Any]:
        all_props = template.properties + template.common_properties
        prop_map = {p.name: p for p in all_props}
        name_map: dict[str, str] = {}
        for p in all_props:
            sanitized = PropertyLabelSanitizer.sanitize(p.name)
            name_map[p.name] = p.name
            if sanitized:
                name_map[sanitized] = p.name
            sanitized_label = PropertyLabelSanitizer.sanitize(p.label)
            if sanitized_label:
                name_map[sanitized_label] = p.name

        result: dict[str, Any] = {}
        for key, value in api_metadata.items():
            real_key = name_map.get(key, key)
            prop = prop_map.get(real_key)
            if not prop:
                continue
            result[real_key] = self._extract_value(value, prop, language)
        return result

    def _extract_value(self, value: Any, prop, language: str) -> Any:
        items = value if isinstance(value, list) else [value]
        extracted: list[Any] = []
        for item in items:
            if isinstance(item, dict):
                if prop.type in (PropertyType.SELECT, PropertyType.MULTI_SELECT):
                    label = self._label_for_thesaurus_value(prop, item.get("value"), language)
                    if label is not None:
                        extracted.append(label)
                elif prop.type == PropertyType.RELATIONSHIP:
                    label = item.get("label") or item.get("value")
                    if label:
                        extracted.append(label)
                elif prop.type == PropertyType.LINK:
                    label = item.get("label")
                    url = item.get("url")
                    if label and url:
                        extracted.append({"label": label, "url": url})
                    elif label or url:
                        extracted.append({"label": label or "", "url": url or ""})
                elif prop.type == PropertyType.GEO_LOCATION:
                    lat = item.get("lat")
                    lon = item.get("lon")
                    if lat is not None and lon is not None:
                        extracted.append([lat, lon])
                elif "value" in item:
                    extracted.append(item["value"])
                elif "label" in item:
                    extracted.append(item["label"])
            else:
                extracted.append(item)

        if len(extracted) == 1 and not _is_repeated_type(prop.type):
            return extracted[0]
        return extracted

    def _label_for_thesaurus_value(self, prop, value: Any, language: str) -> Optional[str]:
        if value is None or prop.content is None:
            return value
        for thesaurus in self._thesauri_repo.get(language=language):
            if thesaurus.id == prop.content:
                for v in thesaurus.values:
                    if v.id == value:
                        return v.label
                break
        return value

    def _coerce_metadata(
        self,
        metadata: dict[str, Any],
        template: Template,
        language: str,
    ) -> dict[str, Any]:
        all_props = template.properties + template.common_properties
        prop_map = {p.name: p for p in all_props}
        name_map: dict[str, str] = {}
        for p in all_props:
            sanitized = PropertyLabelSanitizer.sanitize(p.name)
            name_map[p.name] = p.name
            if sanitized:
                name_map[sanitized] = p.name
            sanitized_label = PropertyLabelSanitizer.sanitize(p.label)
            if sanitized_label:
                name_map[sanitized_label] = p.name

        result: dict[str, Any] = {}
        for raw_key, raw_value in metadata.items():
            key = name_map.get(raw_key, raw_key)
            prop = prop_map.get(key)
            if not prop:
                raise SearchError(f"Metadata property '{raw_key}' is not defined in template '{template.name}'.")
            result[key] = self._coerce_value(raw_value, prop, language)
        return result

    def _coerce_value(self, value: Any, prop, language: str) -> Any:
        prop_type = prop.type
        if prop_type in _SIMPLE_PROPS or prop_type == PropertyType.NUMERIC:
            return _to_value_list(value)
        if prop_type == PropertyType.DATE:
            return _to_value_list(_to_epoch(value))
        if prop_type == PropertyType.MULTI_DATE:
            return [_to_epoch(v) for v in _to_list(value)]
        if prop_type == PropertyType.DATE_RANGE:
            return _to_value_list(_coerce_daterange(value))
        if prop_type == PropertyType.MULTI_DATE_RANGE:
            return [_coerce_daterange(v) for v in _to_list(value)]
        if prop_type == PropertyType.LINK:
            return _to_value_list(_coerce_link(value))
        if prop_type == PropertyType.GEO_LOCATION:
            return _to_value_list(_coerce_geolocation(value))
        if prop_type in (PropertyType.SELECT, PropertyType.MULTI_SELECT):
            if prop_type == PropertyType.SELECT:
                return [_resolve_thesaurus_label(prop, value, self._thesauri_repo, language)]
            return [_resolve_thesaurus_label(prop, v, self._thesauri_repo, language) for v in _to_list(value)]
        if prop_type == PropertyType.RELATIONSHIP:
            if isinstance(value, dict):
                return _to_value_list(value)
            return _to_value_list({"label": str(value)})
        return _to_value_list(value)


def _is_repeated_type(prop_type: PropertyType) -> bool:
    return prop_type in {
        PropertyType.MULTI_DATE,
        PropertyType.MULTI_DATE_RANGE,
        PropertyType.MULTI_SELECT,
    }


def _to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str) and "|" in value and not _looks_like_geo(value):
        return [v for v in value.split("|") if v != ""]
    return [value]


def _to_value_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        if not value:
            return []
        if all(isinstance(v, dict) and "value" in v for v in value):
            return value  # already in envelope form
        if all(isinstance(v, dict) for v in value):
            return value
        return [{"value": v} for v in value]
    return [{"value": value}]


def _to_epoch(value: Any) -> Any:
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, date):
        return int(datetime.combine(value, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp())
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            try:
                return int(float(value))
            except ValueError:
                return value
    return value


def _coerce_daterange(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        if isinstance(value, str) and "->" in value:
            f, t = value.split("->", 1)
            value = {"from": f, "to": t}
        else:
            raise SearchError(f"Date range value must be an object with 'from' and 'to', got {value!r}")
    out: dict[str, Any] = {}
    if value.get("from") is not None:
        out["from"] = _to_epoch(value["from"])
    if value.get("to") is not None:
        out["to"] = _to_epoch(value["to"])
    if "from" not in out and "to" not in out:
        raise SearchError(f"Date range must define at least 'from' or 'to', got {value!r}")
    return out


def _coerce_link(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"label": str(value.get("label", "")), "url": str(value.get("url", ""))}
    if isinstance(value, str) and "|" in value:
        label, url = value.split("|", 1)
        return {"label": label, "url": url}
    if isinstance(value, str):
        return {"label": value, "url": value}
    raise SearchError(f"Link value must be an object with 'label' and 'url' or a 'label|url' string, got {value!r}")


def _coerce_geolocation(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        lat = value.get("lat")
        lon = value.get("lon")
        if lat is None or lon is None:
            raise SearchError(f"Geolocation value must contain 'lat' and 'lon', got {value!r}")
        return {"lat": float(lat), "lon": float(lon), "label": str(value.get("label", ""))}
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return {"lat": float(value[0]), "lon": float(value[1]), "label": ""}
    if isinstance(value, str) and "|" in value:
        lat, lon = value.split("|", 1)
        return {"lat": float(lat), "lon": float(lon), "label": ""}
    raise SearchError(f"Geolocation value must be [lat, lon] or 'lat|lon', got {value!r}")


def _looks_like_geo(value: str) -> bool:
    if "|" not in value:
        return False
    parts = value.split("|", 1)
    try:
        float(parts[0])
        float(parts[1])
        return True
    except ValueError:
        return False


def _resolve_thesaurus_label(
    prop,
    value: Any,
    thesauri_repo: ThesauriRepository,
    language: str,
) -> str:
    if value is None or value == "":
        return value
    if not prop.content:
        raise SearchError(f"Property '{prop.name}' has no thesaurus configured.")
    label_to_id: dict[str, str] = {}
    for thesaurus in thesauri_repo.get(language=language):
        if thesaurus.id == prop.content:
            label_to_id = {v.label: v.id for v in thesaurus.values}
            break
    if value in label_to_id:
        return label_to_id[value]
    for thesaurus in thesauri_repo.get(language=language):
        if thesaurus.id == prop.content:
            for v in thesaurus.values:
                if v.id == value:
                    return v.id
    raise SearchError(
        f"Value '{value}' is not a valid thesaurus label for property '{prop.name}' "
        f"(thesaurus id {prop.content}). Pass a label, not a UUID."
    )
