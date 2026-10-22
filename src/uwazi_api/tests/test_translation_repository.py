"""Unit tests for the translation repository's request shape.

Offline, no mocks: an in-process HTTP double (a real object implementing the
``post`` surface the repository uses) records the request body the repository
would send and answers with a canned 200 payload. This is the convention
``test_entity_repository_save`` established with its in-file HTTP double.

What is pinned: the WHOLE text goes out in ONE request. The text is not
split, so the endpoint receives exactly the text the caller asked to
translate.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from uwazi_api.domain.exceptions import TranslationError
from uwazi_api.use_cases.repositories.translation_repository import TranslationRepository


class _Response:
    """A canned HTTP response (the two attributes the repository reads)."""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.content = json.dumps(payload).encode()


class _RequestAdapter:
    """Records every request body and answers with the canned translation."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def post(self, url: str, data: str, **kwargs: Any) -> _Response:
        self.urls.append(url)
        body = json.loads(data)
        self.bodies.append(body)
        if self.status != 200:
            return _Response(self.status, {"error": "Translation service request failed: Bad Request"})
        return _Response(200, {"translated_text": f"[{body['text']}]"})


class _Http:
    """The minimal slice of the client's HTTP surface the repository touches."""

    def __init__(self, status: int = 200) -> None:
        self.url = "http://instance"
        self.headers: dict[str, str] = {}
        self.request_adapter = _RequestAdapter(status)


class _Graylog:
    """Collects the repository's log lines (the real one logs to graylog)."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message: str) -> None:
        self.lines.append(message)


def _repository(status: int = 200) -> tuple[TranslationRepository, _Http]:
    http = _Http(status)
    http.graylog = _Graylog()  # type: ignore[attr-defined]
    return TranslationRepository(http), http  # type: ignore[arg-type]


# A long text: the shape of a long biography. It must travel in one piece.
LONG_TEXT = " ".join(
    [
        "Debido a su participación en las protestas fue acosada y amenazada por personas afines al partido de gobierno.",
        "Siendo detenida el 9 noviembre de 2021, por agentes motorizados y armados que le interceptaron el carro.",
        "Al día siguiente fue localizada por sus familiares en el Diseño de Auxilio Judicial.",
        "Todos los días la trasladaban a la Dirección de Auxilio Judicial, para que le tomaran entrevista.",
        "Luego la trasladaban a otra sala a interrogatorios donde le tomaban declaración.",
        "En los primeros días de su detención no le dieron alimentos, le quitaron sus pertenencias, incluido su ropa.",
        "Doce días después de su detención, fue trasladada a la courtroom, donde estuvo incomunicada.",
        "El 16 de febrero de 2022, fue trasladada a la audiencia de juicio a las 6:00, a.m. sin desayunar.",
    ]
)


def test_a_long_text_is_sent_as_one_request() -> None:
    repository, http = _repository()

    repository.translate(LONG_TEXT, "es", "en")

    assert len(http.request_adapter.bodies) == 1


def test_the_request_carries_the_whole_text() -> None:
    """Nothing is cut off on the way to the endpoint."""
    repository, http = _repository()

    repository.translate(LONG_TEXT, "es", "en")

    assert http.request_adapter.bodies[0]["text"] == LONG_TEXT


def test_the_request_goes_to_the_translation_endpoint() -> None:
    repository, http = _repository()

    repository.translate(LONG_TEXT, "es", "en")

    assert http.request_adapter.urls == ["http://instance/api/translationService"]


def test_the_request_keeps_the_language_pair() -> None:
    repository, http = _repository()

    repository.translate(LONG_TEXT, "es", "en")

    assert http.request_adapter.bodies[0]["language_from"] == "es"
    assert http.request_adapter.bodies[0]["language_to"] == "en"


def test_the_translation_carries_the_language_pair() -> None:
    repository, _http = _repository()

    translation = repository.translate(LONG_TEXT, "es", "fr")

    assert translation.language_from == "es"
    assert translation.language_to == "fr"


def test_the_translation_is_the_answer_to_the_whole_text() -> None:
    repository, _http = _repository()

    translation = repository.translate(LONG_TEXT, "es", "en")

    assert translation.text == f"[{LONG_TEXT}]"


def test_the_language_pair_is_trimmed() -> None:
    """The domain trims the codes, and the translation reports the trimmed ones."""
    repository, http = _repository()

    translation = repository.translate("Una frase.", " es ", " fr\n")

    assert http.request_adapter.bodies[0]["language_from"] == "es"
    assert http.request_adapter.bodies[0]["language_to"] == "fr"
    assert translation.language_from == "es"
    assert translation.language_to == "fr"


def test_a_refusal_fails_the_whole_call() -> None:
    """A failed translation is never returned as a complete one."""
    repository, _http = _repository(status=400)

    with pytest.raises(TranslationError):
        repository.translate(LONG_TEXT, "es", "en")
