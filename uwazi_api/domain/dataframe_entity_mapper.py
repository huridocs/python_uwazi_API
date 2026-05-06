import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from uwazi_api.domain.attachment import Attachment
from uwazi_api.domain.document import Document
from uwazi_api.domain.entity import Entity
from uwazi_api.use_cases.sanitize_property_label import PropertyLabelSanitizer


class DataFrameEntityMapper:
    BASIC_COLUMNS = {
        "_id",
        "sharedId",
        "title",
        "template",
        "language",
        "published",
        "creationDate",
        "editDate",
        "documents",
        "attachments",
    }

    def __init__(self, prop_type_map: dict[str, str] | None = None, name_map: dict[str, str] | None = None):
        self._prop_type_map = prop_type_map or {}
        self._name_map = name_map or {}

    @staticmethod
    def sanitize_dataframe(df: pd.DataFrame, template: str = "") -> pd.DataFrame:
        df = df.copy()
        default_cols = set(DataFrameEntityMapper.BASIC_COLUMNS)
        sanitized_cols = []
        for col in df.columns:
            if col in default_cols:
                sanitized_cols.append(col)
            else:
                is_geolocation = col.endswith("_geolocation")
                original_col = col[: -len("_geolocation")] if is_geolocation else col
                sanitized = PropertyLabelSanitizer.sanitize(original_col)
                sanitized = sanitized + "_geolocation" if is_geolocation and sanitized else sanitized
                if not sanitized:
                    sanitized = col
                sanitized_cols.append(sanitized)
        df.columns = sanitized_cols
        if template:
            df["template"] = template
        return df

    def map_row_to_entity(self, row: pd.Series) -> Entity:
        entity_dict = self._map_basic_columns(row)
        entity_dict = self._map_documents(entity_dict)
        entity_dict = self._map_attachments(entity_dict)
        entity_dict["metadata"] = self._map_metadata(row)
        return Entity(**entity_dict)

    def _map_basic_columns(self, row: pd.Series) -> dict[str, Any]:
        entity_dict = {}
        for col in self.BASIC_COLUMNS:
            if col in row and pd.notna(row[col]):
                if col == "_id":
                    entity_dict["id"] = row[col]
                elif col == "sharedId":
                    entity_dict["sharedId"] = row[col]
                elif col == "creationDate":
                    entity_dict["creationDate"] = row[col]
                elif col == "editDate":
                    entity_dict["editDate"] = row[col]
                else:
                    entity_dict[col] = row[col]
        return entity_dict

    def _map_documents(self, entity_dict: dict[str, Any]) -> dict[str, Any]:
        if "documents" not in entity_dict:
            return entity_dict
        doc_filenames = [f.strip() for f in str(entity_dict["documents"]).split("|") if f.strip()]
        entity_dict["documents"] = [Document(filename=f) for f in doc_filenames]
        return entity_dict

    def _map_attachments(self, entity_dict: dict[str, Any]) -> dict[str, Any]:
        if "attachments" not in entity_dict:
            return entity_dict
        att_filenames = [f.strip() for f in str(entity_dict["attachments"]).split("|") if f.strip()]
        entity_dict["attachments"] = [Attachment(originalname=f) for f in att_filenames]
        return entity_dict

    def _map_metadata(self, row: pd.Series) -> dict[str, Any]:
        metadata_columns = [col for col in row.index if col not in self.BASIC_COLUMNS and pd.notna(row[col])]
        metadata = {}
        for col in metadata_columns:
            value = row[col]
            prop_key = self._name_map.get(col, col)
            prop_type = self._prop_type_map.get(prop_key)
            metadata[prop_key] = self._parse_property_value(value, prop_type)
        return metadata

    def _parse_property_value(self, value: Any, prop_type: str | None) -> Any:
        if prop_type == "date":
            return self._parse_date_to_timestamp(value)
        if prop_type in ("daterange", "multidaterange"):
            return self._parse_daterange(value)
        if prop_type == "multidate":
            return self._parse_multidate(value)
        if prop_type in ("select", "multiselect"):
            return self._parse_select_value(value)
        if prop_type == "geolocation":
            return self._parse_geolocation(value)
        if prop_type == "relationship":
            return self._parse_relationship_value(value)
        return value

    @staticmethod
    def _parse_date_to_timestamp(value: Any) -> float | None:
        if pd.isna(value):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            value = value.strip()
            for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except ValueError:
                    continue
            try:
                return float(value)
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_daterange(value: Any) -> dict | None:
        if pd.isna(value):
            return None
        value_str = str(value).strip()
        if "|" in value_str:
            parts = value_str.split("|")
            try:
                return {"from": float(parts[0]), "to": float(parts[1])}
            except (ValueError, IndexError):
                return None
        if ":" in value_str:
            parts = value_str.split(":")
            if len(parts) == 2:
                from_ts = DataFrameEntityMapper._parse_date_to_timestamp(parts[0])
                to_ts = DataFrameEntityMapper._parse_date_to_timestamp(parts[1])
                if from_ts and to_ts:
                    return {"from": from_ts, "to": to_ts}
        return None

    @staticmethod
    def _parse_multidate(value: Any) -> list[float] | float | None:
        if isinstance(value, str) and "|" in value:
            timestamps = []
            for v in value.split("|"):
                ts = DataFrameEntityMapper._parse_date_to_timestamp(v)
                if ts is not None:
                    timestamps.append(ts)
            return timestamps if timestamps else None
        return DataFrameEntityMapper._parse_date_to_timestamp(value)

    @staticmethod
    def _parse_select_value(value: Any) -> list[str] | Any:
        if isinstance(value, str) and "|" in value:
            return [v.strip() for v in value.split("|") if v.strip()]
        return value

    @staticmethod
    def _parse_geolocation(value: Any) -> dict | None:
        if pd.isna(value):
            return None
        value_str = str(value).strip()
        if "|" in value_str:
            parts = value_str.split("|")
            try:
                return {"lat": float(parts[0]), "lon": float(parts[1]), "label": ""}
            except (ValueError, IndexError):
                return None
        return None

    @staticmethod
    def _parse_relationship_value(value: Any) -> list[dict] | None:
        if pd.isna(value):
            return None
        value_str = str(value).strip()
        match = re.search(r"\(id:([a-zA-Z0-9_-]+)\)", value_str)
        if match and "|" not in value_str:
            return [{"value": match.group(1)}]
        if "|" in value_str:
            parts = value_str.split("|")
            extracted_ids = []
            for part in parts:
                part = part.strip()
                match = re.search(r"\(id:([a-zA-Z0-9_-]+)\)", part)
                if match:
                    extracted_ids.append(match.group(1))
                else:
                    match = re.match(r"^([a-zA-Z0-9_-]+)$", part)
                    if match:
                        extracted_ids.append(match.group(1))
            if extracted_ids:
                return [{"value": sid} for sid in extracted_ids]
        return [{"value": value_str}]
