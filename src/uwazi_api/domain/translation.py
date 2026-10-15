from typing import Any

from pydantic import BaseModel, field_validator

from uwazi_api.domain.exceptions import TranslationError


class TranslationRequest(BaseModel):
    """One ``POST /api/translationService`` call.

    Mirrors the Uwazi request schema: three required, non-empty strings. The
    server trims each value before validating, so the same trimming happens
    here to reject blank input without spending a round trip.
    """

    text: str
    language_from: str
    language_to: str

    @field_validator("text", "language_from", "language_to")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be empty")
        return trimmed

    def to_payload(self) -> dict[str, str]:
        """The JSON body Uwazi expects (snake_case, same field names)."""
        return {"text": self.text, "language_from": self.language_from, "language_to": self.language_to}


class Translation(BaseModel):
    """A translated text plus the language pair it was produced for.

    Uwazi answers with ``{"translated_text": "..."}``; the languages are not
    echoed back, so they are carried through from the request.
    """

    text: str
    language_from: str
    language_to: str

    @classmethod
    def from_response(cls, payload: Any, language_from: str, language_to: str) -> "Translation":
        """Build a ``Translation`` from a decoded ``/api/translationService`` body.

        Uwazi treats a missing / non-string ``translated_text`` as a failed
        translation (the external service never answered usefully), so it is
        raised as ``TranslationError`` instead of silently returning "".
        """
        if isinstance(payload, dict):
            raw = payload.get("translated_text")
        else:
            raw = payload
        if not isinstance(raw, str):
            raise TranslationError(f"Translation service did not return 'translated_text' (got {type(raw).__name__})")
        return cls(text=raw, language_from=language_from, language_to=language_to)
