from datetime import date, datetime

from pydantic import BaseModel, Field


class DateRange(BaseModel):
    from_: date | None = None
    to: date | None = None

    class Config:
        populate_by_name = True
        extra = "ignore"

    def _to_timestamp(self, d: date) -> float:
        return datetime.combine(d, datetime.min.time()).timestamp()

    def model_dump(self, **kwargs):
        d = super().model_dump(**kwargs)
        if d.get("from_") is not None:
            d["from"] = self._to_timestamp(d.pop("from_"))
        if d.get("to") is not None:
            d["to"] = self._to_timestamp(d.pop("to"))
        return d


class SelectFilter(BaseModel):
    values: list[str] = Field(default_factory=list)

    def model_dump(self, **kwargs):
        d = super().model_dump(**kwargs)
        if not d.get("values"):
            d["values"] = ["missing"]
        return d


class SearchFilters(BaseModel):
    filters: dict[str, DateRange | SelectFilter] = Field(default_factory=dict)

    def add(self, filter_name: str, filter_value: DateRange | SelectFilter) -> None:
        self.filters[filter_name] = filter_value

    class Config:
        populate_by_name = True
        extra = "ignore"
