"""Isolated unit tests for content-addressed text cache keys (per AGENTS.md)."""

from uwazi_admin_agent.domain.text_cache import (
    llm_complete_cache_key,
    llm_map_cache_key,
    translation_cache_key,
)


def test_llm_complete_key_is_deterministic() -> None:
    first = llm_complete_cache_key(namespace="model-a", instruction="Extract", text="hello")
    second = llm_complete_cache_key(namespace="model-a", instruction="Extract", text="hello")
    assert first == second
    assert len(first) == 64


def test_llm_complete_key_changes_with_every_input() -> None:
    base = llm_complete_cache_key(namespace="model-a", instruction="Extract", text="hello")
    assert llm_complete_cache_key(namespace="model-b", instruction="Extract", text="hello") != base
    assert llm_complete_cache_key(namespace="model-a", instruction="Summarize", text="hello") != base
    assert llm_complete_cache_key(namespace="model-a", instruction="Extract", text="world") != base


def test_llm_map_key_covers_the_whole_ordered_batch() -> None:
    base = llm_map_cache_key(namespace="model-a", instruction="Map", items=["a", "b"])
    assert llm_map_cache_key(namespace="model-a", instruction="Map", items=["a", "b"]) == base
    assert llm_map_cache_key(namespace="model-a", instruction="Map", items=["b", "a"]) != base
    assert llm_map_cache_key(namespace="model-a", instruction="Map", items=["a"]) != base


def test_translation_key_covers_text_and_both_languages() -> None:
    base = translation_cache_key(text="hello", language_from="en", language_to="es")
    assert translation_cache_key(text="hello", language_from="en", language_to="es") == base
    assert translation_cache_key(text="hello", language_from="en", language_to="fr") != base
    assert translation_cache_key(text="bye", language_from="en", language_to="es") != base
