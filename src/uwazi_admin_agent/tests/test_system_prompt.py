"""Isolated unit tests for the script-generation system prompt.

Per ``AGENTS.md``: no mocks/stubs, no network. The system prompt is a module-level
string; these are pure string-presence regression tests so the pinned
``query_entities`` return-access forms, the entity/metadata shape, the
dummy-spec rules, and the no-op-on-change-prompt instruction cannot be silently
dropped by a future edit. The bugs they prevent:
- the LLM wrote ``res["summary"]["shared_ids"]`` (subscript) when the search
  result is an OBJECT (``res.summary.shared_ids`` - attribute), raising
  ``TypeError: 'AgentEntitySummary' object is not subscriptable``;
- the LLM grouped/deduped by ``d["metadata"]`` (a dict) or
  ``d["metadata"][prop]`` (a list), raising ``TypeError: unhashable type:
  'list'/'dict'`` (metadata values are ALWAYS arrays);
- the LLM put guessed thesaurus labels (``"draft"``, ``"final"``) in the
  dummy_spec, so Uwazi rejected dummy creation and validation could not run.
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


# --- Entity / metadata shape (Fix A) -----------------------------------------


def test_prompt_states_metadata_values_are_arrays() -> None:
    """`d["metadata"]` is `{prop: [values]}` - every value is a list. The prompt
    must state this so the LLM does not treat a metadata value as a scalar."""
    assert "property_name: [values]" in SYSTEM_PROMPT
    assert "every property's value is an ARRAY (list)" in SYSTEM_PROMPT


def test_prompt_forbids_grouping_by_metadata_dict_or_list() -> None:
    """Grouping by `d["metadata"]` (dict) or `d["metadata"][prop]` (list) raises
    `TypeError: unhashable type`. The prompt must call out both WRONG forms so the
    LLM groups by a scalar (title) instead."""
    assert 'groups.setdefault(d["metadata"], [])' in SYSTEM_PROMPT
    assert "unhashable type: 'dict'" in SYSTEM_PROMPT
    assert 'groups.setdefault(d["metadata"]["document_status"], [])' in SYSTEM_PROMPT
    assert "unhashable type: 'list'" in SYSTEM_PROMPT
    assert 'groups.setdefault(d["title"], [])' in SYSTEM_PROMPT


def test_prompt_teaches_string_key_dedupe() -> None:
    """Deduping value arrays must use a hashable string key (json.dumps), not the
    list/dict itself."""
    assert "json.dumps(v, sort_keys=True" in SYSTEM_PROMPT


# --- Dummy-spec rules (Fix B) ------------------------------------------------


def test_prompt_forbids_thesaurus_properties_in_dummy_spec() -> None:
    """Guessed thesaurus labels (e.g. 'draft', 'final') make Uwazi reject dummy
    creation. The prompt must tell the LLM to use only simple non-thesaurus
    properties in the dummy_spec."""
    assert "non-thesaurus" in SYSTEM_PROMPT
    assert "thesaurus" in SYSTEM_PROMPT
    assert '"draft"' in SYSTEM_PROMPT
    assert '"not a valid thesaurus label"' in SYSTEM_PROMPT


def test_prompt_requires_exact_labels_when_thesaurus_property_needed() -> None:
    """If a thesaurus property is essential, the LLM must discover exact valid
    labels from existing entities first - never guess."""
    assert "EXACT valid labels" in SYSTEM_PROMPT
    assert "never guess" in SYSTEM_PROMPT


def test_prompt_requires_small_dummy_spec() -> None:
    """The dummy spec proves the merge logic, not the production scale. The prompt
    must cap it at a small representative sample."""
    assert "SMALL" in SYSTEM_PROMPT
    assert "2-3 dummies per title group" in SYSTEM_PROMPT
    assert "production scale" in SYSTEM_PROMPT
