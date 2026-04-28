from typing import Optional

import pandas as pd

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import TemplateNotFoundError
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository


def entities_to_dataframe(
    entities: list[Entity],
    template_name: Optional[str] = None,
    template_repo: Optional[TemplateRepository] = None,
) -> pd.DataFrame:
    # Get template to check property types (for relationship handling)
    template = None
    if template_name and template_repo:
        template = template_repo.get_by_id(template_name) or template_repo.get_by_name(template_name)

    # Build a set of relationship property names
    relationship_props = set()
    if template:
        for prop in template.properties + template.common_properties:
            if prop.type == PropertyType.RELATIONSHIP:
                relationship_props.add(prop.name)

    flattened_entities = []
    for entity in entities:
        flattened = {
            "_id": entity.id,
            "sharedId": entity.shared_id,
            "title": entity.title,
            "template": entity.template,
            "language": entity.language,
            "published": entity.published,
            "creationDate": entity.creation_date,
            "editDate": entity.edit_date,
            "documents": "|".join([d.filename for d in entity.documents]),
            "attachments": "|".join([a.originalname for a in entity.attachments]),
        }

        metadata = entity.metadata or {}
        for key, value in metadata.items():
            if isinstance(value, list):
                if len(value) > 0:
                    if isinstance(value[0], dict):
                        extracted_values = []
                        for item in value:
                            # For relationship properties, extract both label and value (sharedId)
                            if key in relationship_props:
                                label = item.get("label", item.get("value", ""))
                                shared_id = item.get("value", "")
                                if label and shared_id:
                                    extracted_values.append(f"{label} (id:{shared_id})")
                                elif shared_id:
                                    extracted_values.append(shared_id)
                            else:
                                if "label" in item:
                                    extracted_values.append(item["label"])
                                elif "value" in item:
                                    extracted_values.append(item["value"])
                            if "parent" in item and "label" in item["parent"] and extracted_values:
                                parent_label = str(item["parent"]["label"])
                                current_val = str(extracted_values[-1])
                                extracted_values[-1] = f"{parent_label}::{current_val}"
                        if len(extracted_values) == 1:
                            flattened[key] = extracted_values[0]
                        elif len(extracted_values) > 1:
                            flattened[key] = "|".join(str(v) for v in extracted_values)
                        else:
                            flattened[key] = None
                    else:
                        flattened[key] = "|".join(str(v) for v in value)
                else:
                    flattened[key] = None
            else:
                flattened[key] = value

        flattened_entities.append(flattened)

    df = pd.DataFrame(flattened_entities)

    if template_name and template_repo:
        template = template_repo.get_by_id(template_name) or template_repo.get_by_name(template_name)
        if not template:
            raise TemplateNotFoundError(f"Template '{template_name}' not found")
        df = _convert_dates(df, template)
        df = _convert_links(df, template)
        df = _convert_geolocations(df, template)

    return df


def _format_timestamp(val, unit):
    try:
        return pd.to_datetime(val, unit=unit, errors="coerce").strftime("%Y/%m/%d")
    except Exception:
        return ""


def _parse_daterange(val):
    if isinstance(val, dict) and "from" in val and "to" in val:
        return f"{_format_timestamp(val['from'], 's')}:{_format_timestamp(val['to'], 's')}"
    return val


def _convert_geolocations(dataframe: pd.DataFrame, template) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    geolocation_columns = set()
    all_props = template.common_properties + template.properties
    for prop in all_props:
        if prop.type == PropertyType.GEO_LOCATION and prop.name in dataframe.columns:
            geolocation_columns.add(prop.name)
    df_copy = dataframe.copy()
    for col in geolocation_columns:
        df_copy[col] = df_copy[col].apply(
            lambda val: f"{val['lat']}|{val['lon']}" if isinstance(val, dict) and "lat" in val and "lon" in val else val
        )
    return df_copy


def _convert_links(dataframe: pd.DataFrame, template) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    link_columns = set()
    all_props = template.common_properties + template.properties
    for prop in all_props:
        if prop.type == PropertyType.LINK and prop.name in dataframe.columns:
            link_columns.add(prop.name)
    df_copy = dataframe.copy()
    for col in link_columns:
        df_copy[col] = df_copy[col].apply(
            lambda val: f"{val['label']}|{val['url']}" if isinstance(val, dict) and "label" in val and "url" in val else val
        )
    return df_copy


def _convert_date_value(val, unit, pattern):
    if pd.isna(val) or val is None:
        return None
    if isinstance(val, str) and "|" in val:
        return "|".join(_convert_date_value(v, unit, pattern) for v in val.split("|"))
    if isinstance(val, (int, float)):
        return pd.to_datetime(val, unit=unit, errors="coerce").strftime(pattern)
    if isinstance(val, str):
        try:
            return pd.to_datetime(float(val), unit=unit, errors="coerce").strftime(pattern)
        except (ValueError, TypeError):
            return val
    return val


def _convert_dates(dataframe: pd.DataFrame, template) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    date_columns = set()
    daterange_columns = set()
    all_props = template.common_properties + template.properties
    for prop in all_props:
        if prop.name not in dataframe.columns:
            continue
        if prop.type == PropertyType.DATE:
            date_columns.add(prop.name)
        elif prop.type == PropertyType.DATE_RANGE:
            daterange_columns.add(prop.name)
    df_copy = dataframe.copy()
    for col in date_columns:
        unit = "ms" if col in ["creationDate", "editDate"] else "s"
        pattern = "%Y/%m/%d %H:%M:%S" if col in ["creationDate", "editDate"] else "%Y/%m/%d"
        df_copy[col] = df_copy[col].apply(lambda v: _convert_date_value(v, unit, pattern))
    for col in daterange_columns:
        df_copy[col] = df_copy[col].apply(_parse_daterange)
    return df_copy
