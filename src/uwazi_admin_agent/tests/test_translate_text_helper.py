"""Unit tests for the sandbox ``translate_text`` helper (§ translations).

Pure, offline, no mocks per the repo testing policy: the wired case uses a
deterministic in-file :class:`TranslationPort` implementation (a pure
transformation of the input text — the convention ``test_llm_script_helpers``
established with its in-file runtime port); the unwired case uses the real
factory with ``None``. What is pinned: the ``AI_TRANSLATED_MARKER`` prefix is
attached by the HELPER (never returned raw by the port), the unwired stub
fails loudly with guidance, and the dummy-mode helper prefixes the marker
without touching the text.
"""

from __future__ import annotations

import asyncio

import pytest

from uwazi_admin_agent.configuration import AI_TRANSLATED_MARKER
from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    _build_translate_text_real_helper,
    _translate_text_noop,
)


class UpperPort(TranslationPort):
    """A deterministic TranslationPort: 'translates' by uppercasing (pure)."""

    async def translate(self, text: str, language_from: str, language_to: str) -> str:
        return text.upper()


def test_real_helper_prepends_marker() -> None:
    translate_text = _build_translate_text_real_helper(UpperPort(), asyncio.new_event_loop())

    assert translate_text("Hello", "en", "fr") == f"{AI_TRANSLATED_MARKER}HELLO"


def test_unwired_helper_raises_loudly() -> None:
    translate_text = _build_translate_text_real_helper(None, asyncio.new_event_loop())

    with pytest.raises(RuntimeError, match="translate_text requires a wired translation_port"):
        translate_text("Hello", "en", "fr")


def test_dummy_helper_prefixes_marker_unchanged() -> None:
    translate_text = _translate_text_noop()

    assert translate_text("Hello", "en", "fr") == f"{AI_TRANSLATED_MARKER}Hello"


def test_real_helper_failure_propagates() -> None:
    class FailingPort(TranslationPort):
        async def translate(self, text: str, language_from: str, language_to: str) -> str:
            raise _error

    _error = RuntimeError("no translation service configured")
    translate_text = _build_translate_text_real_helper(FailingPort(), asyncio.new_event_loop())

    with pytest.raises(RuntimeError, match="no translation service"):
        translate_text("Hello", "en", "fr")
