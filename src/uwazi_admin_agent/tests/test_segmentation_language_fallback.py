"""Unit tests for the segmentation language fallback (pure logic)."""

import asyncio

import pytest

from uwazi_admin_agent.adapters.segmentation_repository_adapter import (
    UwaziSegmentationRepository,
    language_candidates,
)
from uwazi_api.domain.exceptions import SegmentationNotFoundError


def test_language_candidates_orders_requested_then_installed() -> None:
    assert language_candidates("es", ["es", "en", "fr"]) == ["es", "en", "fr"]


def test_language_candidates_drops_duplicates_and_empty() -> None:
    assert language_candidates("en", ["en", "", "fr"]) == ["en", "fr"]


def test_language_candidates_empty_installed_returns_requested() -> None:
    assert language_candidates("pt", []) == ["pt"]


class _Files:
    def __init__(self, ok: str) -> None:
        self._ok = ok
        self.calls: list[str] = []

    def get_segmentation(self, shared_id: str, language: str) -> str:
        self.calls.append(language)
        if language != self._ok:
            raise SegmentationNotFoundError(f"no document for {language}")
        return f"SEG-{language}"


class _Settings:
    def get_languages(self) -> list[object]:
        class _Lang:
            def __init__(self, key: str) -> None:
                self.key = key

        return [_Lang("fr"), _Lang("es"), _Lang("en")]


class _Client:
    def __init__(self, ok: str) -> None:
        self.files = _Files(ok)
        self.settings = _Settings()


def test_get_by_shared_id_falls_back_to_configured_language() -> None:
    client = _Client(ok="spa")
    repo = UwaziSegmentationRepository(client)  # type: ignore[arg-type]
    result = asyncio.run(repo.get_by_shared_id("abc", "eng"))
    assert result == "SEG-spa"
    assert client.files.calls == ["eng", "fra", "spa"]


def test_get_by_shared_id_succeeds_on_requested_language_without_settings_call() -> None:
    client = _Client(ok="en")
    repo = UwaziSegmentationRepository(client)  # type: ignore[arg-type]
    assert asyncio.run(repo.get_by_shared_id("abc", "en")) == "SEG-en"
    assert client.files.calls == ["en"]


def test_get_by_shared_id_reraises_when_no_language_has_a_document() -> None:
    class _NeverOk:
        calls: list[str] = []

        def get_segmentation(self, shared_id: str, language: str) -> str:
            self.calls.append(language)
            raise SegmentationNotFoundError("none")

    class _Client2:
        def __init__(self) -> None:
            self.files = _NeverOk()
            self.settings = _Settings()

    client = _Client2()
    repo = UwaziSegmentationRepository(client)  # type: ignore[arg-type]
    with pytest.raises(SegmentationNotFoundError):
        asyncio.run(repo.get_by_shared_id("abc", "en"))
    assert client.files.calls == ["en", "fra", "spa"]
