"""Unit tests for the translation domain models.

Pure, offline, no mocks: ``TranslationRequest``/``Translation`` are
deterministic transformations of literal values and literal Uwazi payloads.
"""

import pytest
from pydantic import ValidationError

from uwazi_api.domain.exceptions import TranslationError
from uwazi_api.domain.translation import Translation, TranslationRequest

_UWAZI_RESPONSE = {"translated_text": "Name here est un avocat ..."}


def test_request_payload_uses_uwazi_field_names() -> None:
    request = TranslationRequest(text="Hello", language_from="en", language_to="fr")

    assert request.to_payload() == {"text": "Hello", "language_from": "en", "language_to": "fr"}


def test_request_trims_surrounding_whitespace() -> None:
    request = TranslationRequest(text="  Hello  ", language_from=" en ", language_to=" fr\n")

    assert request.to_payload() == {"text": "Hello", "language_from": "en", "language_to": "fr"}


def test_request_rejects_blank_text() -> None:
    with pytest.raises(ValidationError):
        TranslationRequest(text="   ", language_from="en", language_to="fr")


def test_request_rejects_blank_language_codes() -> None:
    with pytest.raises(ValidationError):
        TranslationRequest(text="Hello", language_from="", language_to="fr")

    with pytest.raises(ValidationError):
        TranslationRequest(text="Hello", language_from="en", language_to="  ")


def test_translation_reads_translated_text() -> None:
    translation = Translation.from_response(_UWAZI_RESPONSE, language_from="en", language_to="fr")

    assert translation.text == "Name here est un avocat ..."
    assert translation.language_from == "en"
    assert translation.language_to == "fr"


def test_translation_keeps_empty_translation_text() -> None:
    """An empty string is a real (if useless) answer, not a protocol error."""
    translation = Translation.from_response({"translated_text": ""}, language_from="en", language_to="ar")

    assert translation.text == ""


def test_translation_rejects_missing_translated_text() -> None:
    for payload in ({}, {"translated_text": None}, {"text": "hola"}, None, 7):
        with pytest.raises(TranslationError):
            Translation.from_response(payload, language_from="en", language_to="fr")
