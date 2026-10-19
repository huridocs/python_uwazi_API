"""Isolated unit tests for the prompt-clarification system prompt.

Per ``AGENTS.md``: no mocks/stubs, no network. These are pure string-presence
regression tests. The bug they prevent: the clarification agent treated a
``sharedId`` as language-specific and asked "which shared_id is the English
entity and which is the Spanish one?" — in Uwazi every language row of an
entity shares ONE ``sharedId``.
"""

from uwazi_admin_agent.use_cases.clarify_system_prompt import CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_forbids_language_by_shared_id_question() -> None:
    """The clarification prompt must explicitly reject the per-language-id
    question instead of leaving the LLM to infer Uwazi's language model."""
    assert "sharedId vs language" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "ONE `sharedId` shared by ALL of its language rows" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "NEVER ask" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "which shared_id is the English entity" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_keeps_the_generation_contract_embedded() -> None:
    """The clarification prompt reuses the generation contract rather than a
    drifting capability summary; the language-model rule must reach it through
    that embed too."""
    assert "ENTITY / LANGUAGE MODEL" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "language-INDEPENDENT" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
