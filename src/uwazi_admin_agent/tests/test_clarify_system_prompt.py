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


def test_clarify_prompt_forbids_a_language_question_about_a_shared_value() -> None:
    """The agent used to ask "which language row should receive that change?"
    about a MULTISELECT — a question the data model already answers."""
    assert "WHICH PROPERTY TYPES NEED A LANGUAGE" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "LANGUAGE-INDEPENDENT properties" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "which language row should receive that change?" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "NEVER ask" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_limits_the_language_question_to_per_language_properties() -> None:
    """Only the free-text types (and the title) may make the agent ask."""
    assert "PER-LANGUAGE properties" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "Only per-language properties (and the title) may make you ask a language" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "The entity's TITLE is per-language" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_qualifies_the_instance_languages_question() -> None:
    """The language question is asked only for a per-language change."""
    assert "ONLY when the change is actually per-language" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "there is no language to ask about" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_states_that_the_script_only_proposes() -> None:
    """The agent must know the script RECORDS a review table and the operator
    applies it — otherwise it phrases its questions as if the script writes to
    Uwazi on its own."""
    assert "WHAT THE SCRIPT ACTUALLY DOES" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "does NOT touch Uwazi" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "only RECORDS" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "the operator applies it" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_forbids_the_override_question() -> None:
    """The bug: the agent asked "when the value is the same do you want to
    override the value?" — an apply-time decision the operator makes per row
    in the review table, not an ambiguity of the prompt."""
    assert "NEVER ASK AN APPLY-TIME QUESTION" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "do you want to override it?" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "never as a question about overwriting a value" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_offers_the_suggestion_content_question_instead() -> None:
    """The legitimate question behind the override one: which entities get a
    suggestion — "do you want a suggestion as well?"."""
    assert "Do you want a suggestion as well?" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "WHICH" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "SUGGESTIONS to produce" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "Worked example of the swap" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "RIGHT question" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_never_mentions_a_table_in_its_questions() -> None:
    """The operator answers the questions in the wizard, which has no table: the
    prompt tells the agent to say "suggestions" instead."""
    assert "must never mention a" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_bans_the_other_apply_time_questions() -> None:
    """The rest of the apply-time family the agent was drifting into."""
    assert "Should I apply this now?" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "revertable / backed up / undoable" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "Do you want a dry run / validation first?" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "confirmation, not a clarification" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "what should the budget" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_restatement_is_phrased_as_a_proposal() -> None:
    """The restatement itself must not say "Uwazi will be updated" — the
    script proposes, the operator applies."""
    assert "Do NOT phrase" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "the task will suggest" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_forbids_any_question_about_the_stored_values() -> None:
    """A multi-value question must be about the value extracted from the INPUT,
    never about what is already stored on the entity.

    The bug: the agent asked "which set should be suggested for `states` ..."
    with options including "the extracted states plus any states already linked
    to the entity" / "only the extracted states that are not yet linked". Both
    make the proposal depend on the value the score measures, so the suggestion
    can never be wrong — and the validation sample ends up carrying values the
    document never mentions.
    """
    assert "MULTI-VALUED PROPERTIES" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "REPLACES the property's set" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "from the TASK'S INPUT SOURCES ONLY" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "the extracted values plus the ones already" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "do not ask what should happen to the values an" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "as an outcome of" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "suggestion-emission policy" in CLARIFY_SYSTEM_PROMPT_TEMPLATE


def test_clarify_prompt_states_the_proposal_comes_from_the_input() -> None:
    """The prompt's own preamble carries the same rule, so a question outside
    the multi-value section still cannot be about a stored value."""
    assert "PROPOSAL-IS-COMPUTED-FROM-THE-INPUT" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
    assert "It is NEVER computed from the value the target" in CLARIFY_SYSTEM_PROMPT_TEMPLATE
