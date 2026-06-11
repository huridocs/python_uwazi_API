from datetime import date, datetime
from datetime import timezone
from typing import Any, Optional

from loguru import logger

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import SearchError
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.domain.sanitize_property_label import PropertyLabelSanitizer

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate


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
        return self._build_api_entity(
            template_name=agent_entity.template_name,
            title=agent_entity.title,
            metadata=agent_entity.metadata,
            language=agent_entity.language or language,
            published=agent_entity.published,
            shared_id=agent_entity.shared_id,
        )

    def to_api_for_create(
        self,
        agent_entity: AgentEntityCreate,
        language: str = "en",
    ) -> Entity:
        """Build an API ``Entity`` for a brand-new entity (no ``shared_id``).

        Leaving ``sharedId`` unset signals Uwazi to mint a fresh entity on
        upload rather than overwrite an existing one.
        """
        return self._build_api_entity(
            template_name=agent_entity.template_name,
            title=agent_entity.title,
            metadata=agent_entity.metadata,
            language=agent_entity.language or language,
            published=agent_entity.published,
            shared_id=None,
        )

    def _build_api_entity(
        self,
        template_name: str,
        title: str,
        metadata: dict[str, Any],
        language: str,
        published: Optional[bool],
        shared_id: Optional[str],
    ) -> Entity:
        if not template_name:
            ref = shared_id or title or "<new entity>"
            raise SearchError(f"Entity {ref}: `template_name` is required so metadata can be coerced.")
        template = self._template_repo.get_by_name(template_name) or self._template_repo.get_by_id(template_name)
        if not template:
            raise SearchError(f"Template '{template_name}' not found")

        template_id = template.id or template_name
        coerced_metadata: dict[str, Any] = {}
        if metadata:
            coerced_metadata = self._coerce_metadata(metadata, template, language)

        return Entity(
            sharedId=shared_id,
            title=title,
            template=template_id,
            language=language,
            published=published,
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
            if sanitized and sanitized != p.name:
                name_map[sanitized] = p.name
            name_map[p.label] = p.name
            sanitized_label = PropertyLabelSanitizer.sanitize(p.label)
            if sanitized_label and sanitized_label != p.label:
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
                    shared_id = item.get("value")
                    title = item.get("label")
                    if shared_id or title:
                        extracted.append({"shared_id": shared_id or "", "title": title or ""})
                elif prop.type == PropertyType.LINK:
                    # On-disk envelope: {"value": {"label": ..., "url": ...}}
                    inner = item.get("value") if isinstance(item.get("value"), dict) else item
                    label = inner.get("label")
                    url = inner.get("url")
                    if label and url:
                        extracted.append({"label": label, "url": url})
                    elif label or url:
                        extracted.append({"label": label or "", "url": url or ""})
                elif prop.type == PropertyType.GEO_LOCATION:
                    # On-disk envelope: {"value": {"lat": ..., "lon": ..., "label": ...}}
                    inner = item.get("value") if isinstance(item.get("value"), dict) else item
                    lat = inner.get("lat")
                    lon = inner.get("lon")
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
                found = self._find_label_in_values(thesaurus.values, value)
                if found is not None:
                    return found
                break
        return value

    def _find_label_in_values(self, values: list, target_id: str) -> Optional[str]:
        for v in values:
            if v.id == target_id:
                return v.label
            if v.values:
                found = self._find_label_in_values(v.values, target_id)
                if found is not None:
                    return found
        return None

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
            if sanitized and sanitized != p.name:
                name_map[sanitized] = p.name
            name_map[p.label] = p.name
            sanitized_label = PropertyLabelSanitizer.sanitize(p.label)
            if sanitized_label and sanitized_label != p.label:
                name_map[sanitized_label] = p.name

        result: dict[str, Any] = {}
        for raw_key, raw_value in metadata.items():
            key = name_map.get(raw_key, raw_key)
            prop = prop_map.get(key)
            if not prop:
                raise SearchError(f"Metadata property '{raw_key}' is not defined in template '{template.name}'.")
            if prop.type in (PropertyType.IMAGE, PropertyType.MEDIA):
                logger.info("Skipping image/media property '{}' (type {})", raw_key, prop.type)
                continue
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
            return _to_value_list(_coerce_daterange(value, prop_name=prop.name))
        if prop_type == PropertyType.MULTI_DATE_RANGE:
            return [_coerce_daterange(v, prop_name=prop.name) for v in _to_list(value)]
        if prop_type == PropertyType.LINK:
            return _to_value_list(_coerce_link(value, prop_name=prop.name))
        if prop_type == PropertyType.GEO_LOCATION:
            return _to_value_list(_coerce_geolocation(value, prop_name=prop.name))
        if prop_type in (PropertyType.SELECT, PropertyType.MULTI_SELECT):
            if prop_type == PropertyType.SELECT:
                return [_resolve_thesaurus_label(prop, value, self._thesauri_repo, language)]
            return [_resolve_thesaurus_label(prop, v, self._thesauri_repo, language) for v in _to_list(value)]
        if prop_type == PropertyType.RELATIONSHIP:
            return _coerce_relationship(value, prop_name=prop.name)
        return _to_value_list(value)


def _is_repeated_type(prop_type: PropertyType) -> bool:
    return prop_type in {
        PropertyType.MULTI_DATE,
        PropertyType.MULTI_DATE_RANGE,
        PropertyType.MULTI_SELECT,
        PropertyType.RELATIONSHIP,
    }


def _relationship_shared_id(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        # Accept the LLM-facing read shape ({shared_id, title}), the on-disk
        # envelope ({value, label}), and a few common aliases.
        for key in ("shared_id", "sharedId", "value", "entity"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _coerce_relationship(value: Any, prop_name: str = "") -> list[dict[str, Any]]:
    """Normalize a relationship value into the Uwazi envelope ``[{"value": sharedId}]``.

    The agent provides the related entities by their ``shared_id``. Accepted
    shapes: a single ``shared_id`` string, a list of ``shared_id`` strings, a
    dict with a ``shared_id`` (or ``value``) key, or a list of such dicts (this
    handles the on-read shape ``[{"shared_id": ..., "title": ...}]`` being
    echoed back). Only the ``shared_id`` is sent; Uwazi resolves the title.
    """
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    result: list[dict[str, Any]] = []
    for item in items:
        shared_id = _relationship_shared_id(item)
        if not shared_id:
            raise SearchError(
                f"Relationship value for property '{prop_name}' must be a related entity "
                "``shared_id`` (string), a list of shared_ids, or "
                '`{"shared_id": "<id>"}` objects. Search for the target entities to get '
                f"their shared_id, got {item!r}"
            )
        result.append({"value": shared_id})
    return result


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


def _coerce_daterange(value: Any, prop_name: str = "") -> dict[str, Any]:
    if isinstance(value, list) and len(value) == 1:
        # Defensive: accept [envelope] and a single-element list wrapping the value.
        value = value[0]
    if isinstance(value, dict) and "value" in value and isinstance(value["value"], dict):
        # Defensive: accept the on-disk envelope [{"value": {"from": ..., "to": ...}}]
        # shape in case the LLM echoes back what it saw on read.
        value = value["value"]
    if not isinstance(value, dict):
        if isinstance(value, str) and "->" in value:
            f, t = value.split("->", 1)
            value = {"from": f, "to": t}
        else:
            raise SearchError(
                f"Date range value for property '{prop_name}' must be "
                "`{'from': 'YYYY-MM-DD', 'to': 'YYYY-MM-DD'}` or 'YYYY-MM-DD->YYYY-MM-DD', "
                f"got {value!r}"
            )
    out: dict[str, Any] = {}
    if value.get("from") is not None:
        out["from"] = _to_epoch(value["from"])
    if value.get("to") is not None:
        out["to"] = _to_epoch(value["to"])
    if "from" not in out and "to" not in out:
        raise SearchError(f"Date range for property '{prop_name}' must define at least 'from' or 'to', got {value!r}")
    return out


def _coerce_link(value: Any, prop_name: str = "") -> dict[str, Any]:
    if isinstance(value, list) and len(value) == 1:
        # Defensive: accept [envelope] and a single-element list wrapping the value.
        value = value[0]
    if isinstance(value, dict) and "value" in value and isinstance(value["value"], dict):
        # Defensive: accept the on-disk envelope [{"value": {"label": ..., "url": ...}}]
        # shape in case the LLM echoes back what it saw on read.
        value = value["value"]
    if isinstance(value, dict):
        return {"label": str(value.get("label", "")), "url": str(value.get("url", ""))}
    if isinstance(value, str) and "|" in value:
        label, url = value.split("|", 1)
        return {"label": label, "url": url}
    if isinstance(value, str):
        return {"label": value, "url": value}
    raise SearchError(
        f"Link value for property '{prop_name}' must be "
        "`{'label': '<text>', 'url': '<url>'}` or '<text>|<url>', got {value!r}"
    )


def _coerce_geolocation(value: Any, prop_name: str = "") -> dict[str, Any]:
    """Normalize a geolocation value into the Uwazi envelope ``{lat, lon, label}``.

    Accepted input shapes (any of these works on the way in):
      * ``{"lat": <float>, "lon": <float>}`` (a dict)
      * ``[<float>, <float>]`` or ``(<float>, <float>)`` (a list/tuple)
      * ``"<float>|<float>"`` (a string)
      * ``{"value": {"lat": ..., "lon": ..., "label": ...}}`` (the Uwazi
        on-disk envelope; the ``label`` key is optional and is preserved)
      * ``[{"lat": ..., "lon": ..., "label": ...}, ...]`` (a list of
        on-disk envelopes; the first one is used). This handles the
        common LLM round-trip mistake of re-emitting the read shape.
      * ``[[<float>, <float>], ...]`` (a list of ``[lat, lon]`` pairs, the
        read shape the LLM sees; the first pair is used).

    The ``label`` key, when present, is preserved; when absent, it
    defaults to an empty string. The function never raises on the
    defensive envelope unwrap — it falls through to the standard error.
    """
    # Defensive: unwrap the on-disk envelope {"value": {"lat": ..., "lon": ...}}
    if isinstance(value, dict) and set(value.keys()) == {"value"} and isinstance(value["value"], dict):
        value = value["value"]
    if (
        isinstance(value, list)
        and len(value) >= 1
        and all(isinstance(item, (list, tuple)) and len(item) >= 2 and not isinstance(item[0], dict) for item in value)
    ):
        # Read shape: a list of [lat, lon] pairs. Use the first.
        first = value[0]
        return {"lat": float(first[0]), "lon": float(first[1]), "label": ""}
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        # [envelope] -> envelope
        if "lat" in value[0] and "lon" in value[0]:
            value = value[0]
    if isinstance(value, dict):
        lat = value.get("lat")
        lon = value.get("lon")
        if lat is None or lon is None:
            raise SearchError(f"Geolocation value for property '{prop_name}' must contain 'lat' and 'lon', got {value!r}")
        return {"lat": float(lat), "lon": float(lon), "label": str(value.get("label", ""))}
    if isinstance(value, (list, tuple)) and len(value) >= 2 and not isinstance(value[0], dict):
        return {"lat": float(value[0]), "lon": float(value[1]), "label": ""}
    if isinstance(value, str) and "|" in value and _looks_like_geo(value):
        lat, lon = value.split("|", 1)
        return {"lat": float(lat), "lon": float(lon), "label": ""}
    raise SearchError(
        f"Geolocation value for property '{prop_name}' must be one of "
        "`[lat, lon]`, `{'lat': <float>, 'lon': <float>}`, or '<lat>|<lon>', "
        f"got {value!r}"
    )


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
            _build_label_map(thesaurus.values, label_to_id)
            break
    if value in label_to_id:
        return label_to_id[value]
    for thesaurus in thesauri_repo.get(language=language):
        if thesaurus.id == prop.content:
            for v in _flatten_values(thesaurus.values):
                if v.id == value:
                    return v.id
    raise SearchError(
        f"Value '{value}' is not a valid thesaurus label for property '{prop.name}' "
        f"(thesaurus id {prop.content}). Pass a label, not a UUID."
    )


def _build_label_map(values: list, label_map: dict[str, str]) -> None:
    for v in values:
        label_map[v.label] = v.id
        if v.values:
            _build_label_map(v.values, label_map)


def _flatten_values(values: list) -> list:
    result: list = []
    for v in values:
        result.append(v)
        if v.values:
            result.extend(_flatten_values(v.values))
    return result
