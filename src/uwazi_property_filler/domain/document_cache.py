"""In-memory cache of the documents fetched by ``Connect & Refresh``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from pydantic import ValidationError

from uwazi_property_filler.domain.pdf_item import PdfItem


@dataclass
class DocumentCache:
    """Documents per filter bucket, split pending/validated.

    Filled once per ``Connect & Refresh``; every later list render, filter
    change, count, and post-validate refresh is served from here without
    touching Uwazi again. Key ``None`` is the unfiltered "ALL" bucket.
    """

    template: str
    language: str
    pending: dict[str | None, list[PdfItem]] = field(default_factory=dict)
    validated: dict[str | None, list[PdfItem]] = field(default_factory=dict)

    def split(self, filter_value: str | None) -> tuple[list[PdfItem], list[PdfItem]]:
        """(pending, validated) copies for one filter bucket."""
        return list(self.pending.get(filter_value, ())), list(self.validated.get(filter_value, ()))

    def count(self, filter_value: str | None) -> int:
        """Total documents (pending + validated) in one filter bucket."""
        return len(self.pending.get(filter_value, ())) + len(self.validated.get(filter_value, ()))

    def values(self) -> list[str]:
        """All non-``None`` filter bucket keys, pending side order first."""
        seen: list[str] = []
        for key in [*self.pending.keys(), *self.validated.keys()]:
            if key is not None and key not in seen:
                seen.append(key)
        return seen

    def total(self) -> int:
        """Total documents across all buckets (each entity counted per value)."""
        return sum(len(items) for items in self.pending.values()) + sum(len(items) for items in self.validated.values())

    def mark_validated(self, shared_id: str) -> None:
        """Move one document from pending to validated in every bucket holding it."""
        for value, items in self.pending.items():
            item = next((i for i in items if i.shared_id == shared_id), None)
            if item is not None:
                items.remove(item)
                self.validated.setdefault(value, []).append(item)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly shape (buckets as a list so ``None`` keys survive)."""

        def buckets(source: dict[str | None, list[PdfItem]]) -> list[dict[str, Any]]:
            return [
                {"value": value, "items": [item.model_dump(mode="json") for item in items]}
                for value, items in source.items()
            ]

        return {
            "template": self.template,
            "language": self.language,
            "pending": buckets(self.pending),
            "validated": buckets(self.validated),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DocumentCache:
        """Inverse of :meth:`to_dict`; raises on malformed input."""

        def buckets(raw: Any) -> dict[str | None, list[PdfItem]]:
            return {entry["value"]: [PdfItem.model_validate(item) for item in entry["items"]] for entry in raw}

        return cls(
            template=str(data["template"]),
            language=str(data["language"]),
            pending=buckets(data.get("pending", [])),
            validated=buckets(data.get("validated", [])),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> DocumentCache | None:
        """Parse a snapshot, returning ``None`` on any malformed input."""
        try:
            return cls.from_dict(json.loads(text))
        except (json.JSONDecodeError, ValidationError, KeyError, TypeError, AttributeError):
            return None
