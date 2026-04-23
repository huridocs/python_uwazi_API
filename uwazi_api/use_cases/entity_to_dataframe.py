from typing import List, Optional

import pandas as pd

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import TemplateNotFoundError
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository


def entities_to_dataframe(
    entities: List[Entity],
    template_id: Optional[str] = None,
    template_repo: Optional[TemplateRepository] = None,
) -> pd.DataFrame:
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
            "attachments": "|".join([a.filename for a in entity.attachments]),
        }

        metadata = entity.metadata or {}
        for key, value in metadata.items():
            if isinstance(value, list) and len(value) > 0:
                if isinstance(value[0], dict):
                    extracted_values = []
                    for item in value:
                        if "label" in item:
                            extracted_values.append(item["label"])
                        elif "value" in item:
                            extracted_values.append(item["value"])
                        if "parent" in item and "label" in item["parent"]:
                            extracted_values[-1] = item["parent"]["label"] + "::" + str(extracted_values[-1])
                    if len(extracted_values) == 1:
                        flattened[key] = extracted_values[0]
                    elif len(extracted_values) > 1:
                        flattened[key] = "|".join(extracted_values)
                    else:
                        flattened[key] = None
                else:
                    flattened[key] = value
            else:
                flattened[key] = None

        flattened_entities.append(flattened)

    df = pd.DataFrame(flattened_entities)

    if template_id and template_repo:
        template = template_repo.get_by_id(template_id)
        if not template:
            raise TemplateNotFoundError(f"Template {template_id} not found")
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
        if prop.type == "geolocation" and prop.name in dataframe.columns:
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
        if prop.type == "link" and prop.name in dataframe.columns:
            link_columns.add(prop.name)
    df_copy = dataframe.copy()
    for col in link_columns:
        df_copy[col] = df_copy[col].apply(
            lambda val: f"{val['label']}|{val['url']}" if isinstance(val, dict) and "label" in val and "url" in val else val
        )
    return df_copy


def _convert_dates(dataframe: pd.DataFrame, template) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    date_columns = set()
    daterange_columns = set()
    all_props = template.common_properties + template.properties
    for prop in all_props:
        if prop.name not in dataframe.columns:
            continue
        if prop.type == "date":
            date_columns.add(prop.name)
        elif prop.type == "daterange":
            daterange_columns.add(prop.name)
    df_copy = dataframe.copy()
    for col in date_columns:
        unit = "ms" if col in ["creationDate", "editDate"] else "s"
        pattern = "%Y/%m/%d %H:%M:%S" if col in ["creationDate", "editDate"] else "%Y/%m/%d"
        df_copy[col] = pd.to_datetime(df_copy[col], unit=unit, errors="coerce").dt.strftime(pattern)
    for col in daterange_columns:
        df_copy[col] = df_copy[col].apply(_parse_daterange)
    return df_copy
