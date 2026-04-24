from enum import Enum


class PropertyType(str, Enum):
    TEXT = "text"
    DATE = "date"
    SELECT = "select"
    NUMERIC = "numeric"
    DATE_RANGE = "daterange"
    MULTI_DATE = "multidate"
    LINK = "link"
    IMAGE = "image"
    MULTI_DATE_RANGE = "multidaterange"
    MARKDOWN = "markdown"
    MEDIA = "media"
    GENERATED_ID = "generatedid"
    MULTI_SELECT = "multiselect"
    GEO_LOCATION = "geolocation"
    RELATIONSHIP = "relationship"
