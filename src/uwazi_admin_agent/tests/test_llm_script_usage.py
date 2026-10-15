"""Isolated unit tests for the apply-script LLM usage analyzer (per AGENTS.md).

Pure text analysis: no Uwazi instance, no network, no mocks — literal script
sources and plain assertions on the estimated per-entity call counts.
"""

from uwazi_admin_agent.domain.llm_script_usage import LlmScriptUsage, apply_script_llm_usage


def test_no_llm_calls_reported_for_a_plain_script() -> None:
    usage = apply_script_llm_usage(
        """
d = query_entities_full(mode="by_template", template_name="Case")
update_entities([{"shared_id": TARGET_SHARED_ID, "metadata": {"x": d[0]["title"]}}])
result = "ok"
"""
    )
    assert usage == LlmScriptUsage(llm_complete_sites=0, llm_map_sites=0)
    assert usage.uses_llm is False
    assert usage.calls_per_entity == 0


def test_each_llm_complete_site_is_one_call_per_entity() -> None:
    usage = apply_script_llm_usage(
        """
a = llm_complete("Extract date", text)
b = llm_complete("Summarize", text)
result = a + b
"""
    )
    assert usage.llm_complete_sites == 2
    assert usage.llm_map_sites == 0
    assert usage.calls_per_entity == 2
    assert usage.uses_llm is True


def test_each_llm_map_site_counts_as_at_least_one_call() -> None:
    usage = apply_script_llm_usage(
        """
outs = llm_map("Label", items)
result = outs
"""
    )
    assert usage.llm_map_sites == 1
    assert usage.calls_per_entity == 1
    assert usage.uses_llm is True


def test_mixed_sites_sum() -> None:
    usage = apply_script_llm_usage(
        """
one = llm_complete("Extract", text)
many = llm_map("Label", items)
two = llm_complete ("spaced variant", text)
result = (one, many, two)
"""
    )
    assert usage.llm_complete_sites == 2
    assert usage.llm_map_sites == 1
    assert usage.calls_per_entity == 3


def test_mentions_without_call_parentheses_do_not_count() -> None:
    usage = apply_script_llm_usage(
        """
# the helper is llm_map, not llm_mapx
result = "see llm_complete docs"
"""
    )
    assert usage.calls_per_entity == 0
    assert usage.uses_llm is False


def test_overcounting_is_safe_direction() -> None:
    """A call site inside a string counts too — the estimate may only over-
    project, never under-charge, the operator's approved budget."""
    usage = apply_script_llm_usage('result = "call llm_complete(instruction, text) like this"')
    assert usage.llm_complete_sites == 1
