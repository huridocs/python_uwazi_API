"""Isolated unit tests for the script-generation system prompt.

Per ``AGENTS.md``: no mocks/stubs, no network. The system prompt is a module-level
string; these are pure string-presence regression tests so the pinned
``query_entities`` return-access forms and the no-op-on-change-prompt instruction
cannot be silently dropped by a future edit. The bug they prevent: the LLM wrote
``res[\"summary\"][\"shared_ids\"]`` (subscript) when the search result is an OBJECT
(``res.summary.shared_ids`` - attribute), raising ``TypeError: 'AgentEntitySummary'
object is not subscriptable`` and wasting a validation attempt.
"""

from uwazi_admin_agent.use_cases.system_prompt import SYSTEM_PROMPT


def test_prompt_pins_search_result_attribute_access() -> None:
    """The prompt must show the correct ATTRIBUTE access form for search modes."""
    assert "res.summary.shared_ids" in SYSTEM_PROMPT
    assert "res.examples" in SYSTEM_PROMPT


def test_prompt_forbids_subscript_on_search_result() -> None:
    """The prompt must explicitly call out the WRONG subscript form that raises
    TypeError, so the LLM stops writing dict-subscript access."""
    assert 'res["summary"]' in SYSTEM_PROMPT
    assert 'res.summary["shared_ids"]' in SYSTEM_PROMPT
    assert "TypeError" in SYSTEM_PROMPT


def test_prompt_pins_by_ids_dict_subscript_access() -> None:
    """``by_ids`` returns a list[dict]; the prompt must show subscript access there
    (the opposite of the search-result object case)."""
    assert 'd["shared_id"]' in SYSTEM_PROMPT
    assert 'd["metadata"]' in SYSTEM_PROMPT
    assert "list[dict]" in SYSTEM_PROMPT


def test_prompt_warns_noop_pass_on_change_prompt() -> None:
    """A 0-diff PASS on a change-prompt is a no-op false pass; the prompt must tell
    the LLM to treat it as a FAIL and fix the script."""
    assert "0 diffs" in SYSTEM_PROMPT
    assert "no-op" in SYSTEM_PROMPT.lower() or "did nothing" in SYSTEM_PROMPT.lower()


def test_prompt_reinforces_attribute_access_in_merge_sections() -> None:
    """The MERGE TASKS and MULTI-GROUP MERGE sections must reinforce the attribute
    access at the point of use, not just in the EXECUTION SANDBOX block."""
    # MERGE TASKS step 1 and MULTI-GROUP step 2a both reference summary.shared_ids.
    assert SYSTEM_PROMPT.count("summary.shared_ids") >= 2
    assert "ATTRIBUTE access" in SYSTEM_PROMPT
