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


def test_prompt_lists_random_in_bound_stdlib() -> None:
    """`random` is bound in the exec namespace; the prompt's stdlib list must name it
    so the LLM uses `random.randint/choice` for 'fill with random information'
    creates instead of reaching for `__import__` or a hand-rolled pseudo-random."""
    assert "`random`" in SYSTEM_PROMPT


def test_prompt_pins_datetime_is_the_module() -> None:
    """The bound `datetime` is the MODULE, not the class. The prompt must pin the
    correct idiom (`datetime.datetime.now()`) and call out the WRONG forms that
    raise AttributeError (`datetime.datetime.datetime...`) and the
    module-has-no-`now` form (`datetime.now()`) - the LLM confused both in live
    runs."""
    assert "`datetime` is the MODULE" in SYSTEM_PROMPT
    assert "datetime.datetime.now()" in SYSTEM_PROMPT
    assert "datetime.timedelta(days=3)" in SYSTEM_PROMPT
    assert "datetime.now()" in SYSTEM_PROMPT  # called out as WRONG
    assert "datetime.datetime.datetime.now()" in SYSTEM_PROMPT  # called out as WRONG


def test_prompt_teaches_schema_inspection_before_writing() -> None:
    """The prompt must direct the LLM to inspect the template (property names/types/
    required/thesaurus_name) and thesauri (exact labels) BEFORE writing a
    create/update script - the capability gap that caused guessed property names."""
    assert "SCHEMA INSPECTION FIRST" in SYSTEM_PROMPT
    assert "get_templates_by_names" in SYSTEM_PROMPT
    assert "get_thesauris_by_names" in SYSTEM_PROMPT
    assert "list_templates" in SYSTEM_PROMPT
    assert "list_thesauri" in SYSTEM_PROMPT
    assert "not a valid thesaurus label" in SYSTEM_PROMPT


def test_prompt_has_create_tasks_section() -> None:
    """A CREATE TASKS section must exist and pin the create shape: inspect first,
    build dicts with `metadata` arrays, call `create_entities`, set `result`."""
    assert "CREATE TASKS" in SYSTEM_PROMPT
    assert "create_entities(list_of_dicts, language)" in SYSTEM_PROMPT
    # Create must use inspected property names, never guess.
    assert "not defined in template" in SYSTEM_PROMPT


def test_prompt_flags_create_noop_false_pass() -> None:
    """A CREATE prompt that yields 0 created dummies is a FAIL; the prompt must say so
    and point at `created_shared_ids` / `before=None` diffs so the LLM doesn't
    emit a 0-create no-op."""
    assert "FALSE-PASS GUARD" in SYSTEM_PROMPT
    assert "created_shared_ids" in SYSTEM_PROMPT
    assert "before=None" in SYSTEM_PROMPT


def test_prompt_reinforces_noop_for_create_in_validation() -> None:
    """The VALIDATION 0-diff paragraph must cross-reference create tasks so a 0-create
    pass is treated as a FAIL there too."""
    assert "CREATE TASKS / FALSE-PASS GUARD" in SYSTEM_PROMPT


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
    """Guessed thesaurus labels make Uwazi reject dummy creation. The prompt must
    keep `select`/`multiselect` (and the `not a valid thesaurus label` rejection) in
    the dummy-spec AVOID set. (Rewritten to also exclude date-family + relationship;
    the thesaurus-rejection behavior is still pinned.)"""
    assert "thesaurus" in SYSTEM_PROMPT
    assert "`select` / `multiselect`" in SYSTEM_PROMPT
    assert "not a valid thesaurus label" in SYSTEM_PROMPT


def test_prompt_requires_exact_labels_when_thesaurus_property_needed() -> None:
    """If a non-simple property is essential, the LLM must discover its exact valid
    value/label from existing entities first - never guess."""
    assert "EXACT valid" in SYSTEM_PROMPT
    assert "never guess" in SYSTEM_PROMPT


def test_prompt_requires_small_dummy_spec() -> None:
    """The dummy spec proves the merge logic, not the production scale. The prompt
    must cap it at a small representative sample."""
    assert "SMALL" in SYSTEM_PROMPT
    assert "2-3 dummies per title group" in SYSTEM_PROMPT
    assert "production scale" in SYSTEM_PROMPT


# --- Metadata WRITE shape (date/select scalar, not array) -------------------


def test_prompt_distinguishes_read_and_write_metadata_shapes() -> None:
    """The ENTITY SHAPE (arrays) is the READ shape from by_ids; the WRITE shape for
    single-value properties is a SCALAR. The prompt must say so explicitly so the
    LLM stops wrapping single-value properties in arrays on create/update."""
    assert "ARRAY shape above is the READ shape" in SYSTEM_PROMPT
    assert "METADATA WRITE SHAPE" in SYSTEM_PROMPT


def test_prompt_pins_date_write_shape_is_scalar_not_array() -> None:
    """A `date` property on write takes a SCALAR ISO string; the platform coerces it
    to a numeric timestamp. Wrapping it in an array (`["2020-01-01"]`) leaves the
    string intact and Uwazi rejects it with `must have numeric timestamp values,
    got str` - the exact live failure on create-entities.yaml. The prompt must pin
    the scalar form and call out the WRONG array form with the live error."""
    assert "`date`" in SYSTEM_PROMPT and '"YYYY-MM-DD"' in SYSTEM_PROMPT
    assert "must have numeric timestamp values, got str" in SYSTEM_PROMPT
    assert "do NOT wrap" in SYSTEM_PROMPT and "a single date in an array" in SYSTEM_PROMPT


def test_prompt_pins_select_write_shape_is_scalar_not_array() -> None:
    """A single `select` property on write takes a SCALAR thesaurus label, not
    `["label"]`. The mapper rejects an array."""
    assert "`select`" in SYSTEM_PROMPT
    assert 'SCALAR thesaurus label, NOT `["label"]`' in SYSTEM_PROMPT


def test_prompt_create_tasks_uses_write_shape_not_array_shape() -> None:
    """The CREATE TASKS section must direct the LLM to the WRITE SHAPE (scalars for
    single-value types) and must NOT repeat the old 'every value is an ARRAY'
    instruction for create values (that caused the date failure)."""
    assert "Follow the METADATA WRITE SHAPE" in SYSTEM_PROMPT
    assert 'date`->`"YYYY-MM-DD"` (SCALAR string, NOT `["YYYY-MM-DD"]`' in SYSTEM_PROMPT


# --- No-import contract (import random / import datetime) -------------------


def test_prompt_forbids_importing_bound_modules_with_example() -> None:
    """The LLM wrote `import random` at the top of the emitted script (the bound
    namespace has no `__import__`), failing execute with `ImportError: __import__
    not found`. The prompt must call out the concrete WRONG `import random` /
    `import datetime` lines and require ZERO import lines."""
    assert "import random" in SYSTEM_PROMPT  # the WRONG example
    assert "import datetime" in SYSTEM_PROMPT
    assert "ImportError: __import__ not found" in SYSTEM_PROMPT
    assert "ZERO `import` lines" in SYSTEM_PROMPT


# --- Dummy-spec excludes date-family; create-task needs >=1 dummy -----------


def test_prompt_dummy_spec_avoids_date_family() -> None:
    """The dummy_spec must use text/numeric only; date-family properties need
    numeric-timestamp coercion and a guessed string fails creation. The prompt
    must list date/daterange/multidate/multidaterange in the dummy-spec AVOID set."""
    assert "`text` or `numeric` ONLY" in SYSTEM_PROMPT
    for token in ("`date`", "`daterange`", "`multidate`", "`multidaterange`"):
        assert token in SYSTEM_PROMPT


def test_prompt_create_tasks_requires_at_least_one_dummy() -> None:
    """An empty dummy_spec makes the harness error with 'No dummy entities were
    created'; the prompt must tell the LLM to pass >=1 minimal dummy for a create
    task (the script creates the real targets)."""
    assert "REQUIRES at least one dummy" in SYSTEM_PROMPT
    assert "No dummy entities were created" in SYSTEM_PROMPT
