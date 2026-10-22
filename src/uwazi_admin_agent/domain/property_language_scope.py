"""Which Uwazi property types are PER-LANGUAGE, and which are not (pure).

The prompt-clarification agent used to ask "Which language row of
``lii2m1m62f9`` should receive that change?" for a MULTISELECT change — a
question the data model already answers. In Uwazi only the free-text types are
stored per language row; every other type is ONE value on the entity, shared by
all of its language rows. Writing a select therefore needs no language
decision at all, and asking for one is a question the instance cannot answer.

This module is the SINGLE SOURCE OF TRUTH for that rule, so the prompt the
clarification agent reads and the code that records the change cannot drift
apart: the same constant drives the schema context's rendering and the
system prompt's wording.

The rule, in Uwazi's own terms:

- PER-LANGUAGE: ``text`` and ``richtext``. A translated paragraph is one
  ``{"value", "language"}`` entry per language on ONE entity-language row, so
  a translation task genuinely has to pick the language it writes.
- LANGUAGE-INDEPENDENT: every other type — ``select``, ``multiselect``,
  ``date``, ``daterange``, ``multidate``, ``multidaterange``, ``numeric``,
  ``link``, ``geolocation``, ``relationship``, ``image``, ``media``,
  ``generatedid``, and the template-only ``preview``/``nested``. One value,
  shared by every language row of the entity.

The title is per language row too, but it is not a PROPERTY TYPE — it is the
entity's ``title`` field, which the rule below covers separately.
"""

from __future__ import annotations

# The property types whose value is stored PER LANGUAGE ROW. A change to one of
# these is written into ONE language row of the entity, and the operator's
# prompt has to say which (or the agent asks).
PER_LANGUAGE_PROPERTY_TYPES: frozenset[str] = frozenset({"text", "richtext"})

# The property types whose value is ONE value shared by EVERY language row of
# the entity. Writing one of these needs no language decision.
LANGUAGE_INDEPENDENT_PROPERTY_TYPES: frozenset[str] = frozenset(
    {
        "select",
        "multiselect",
        "date",
        "daterange",
        "multidate",
        "multidaterange",
        "numeric",
        "link",
        "geolocation",
        "relationship",
        "image",
        "media",
        "generatedid",
        "preview",
        "nested",
    }
)

# The title is the entity's own per-language field (Uwazi stores one title per
# language row), so a title change is a per-language change — but it is not a
# PROPERTY TYPE, which is why it gets its own name here.
TITLE_IS_PER_LANGUAGE: bool = True


def is_per_language_property_type(property_type: str) -> bool:
    """Whether a property type's value lives on ONE language row (pure).

    ``property_type`` is the lowercase Uwazi name the template definitions
    carry ("text", "multiselect", ...). An unknown type is treated as
    LANGUAGE-INDEPENDENT: that is the safe reading, because a value that is
    not known to be per-language is not a reason to ask the operator which
    language row to write.
    """
    return str(property_type or "").strip().lower() in PER_LANGUAGE_PROPERTY_TYPES


def is_language_independent_property_type(property_type: str) -> bool:
    """Whether a property type's value is shared by every language row (pure).

    The inverse of :func:`is_per_language_property_type` for the types the
    instance actually defines; an unknown type reads as language-independent
    (see :func:`is_per_language_property_type`).
    """
    return not is_per_language_property_type(property_type)


def language_scope_label(property_type: str) -> str:
    """How a property type's language scope reads in the schema (pure).

    The words the clarification agent reads next to each property, so it can
    tell a per-language property from a shared one without a reference table.
    """
    if is_per_language_property_type(property_type):
        return "per-language"
    return "all languages"


def language_scope_rule() -> str:
    """The rule as one block of text for the prompt (pure).

    One rendering of the rule, used by the schema context and the system
    prompt, so the two never disagree about which types are per-language.
    """
    per_language = ", ".join(sorted(PER_LANGUAGE_PROPERTY_TYPES))
    language_independent = ", ".join(sorted(LANGUAGE_INDEPENDENT_PROPERTY_TYPES))
    return (
        "PROPERTY LANGUAGE SCOPE (do NOT ask which language row to write):\n"
        f"  - PER-LANGUAGE types (one value per language row): {per_language}.\n"
        "    A change to one of these is written into ONE language row of the entity,\n"
        "    so it genuinely needs a language — ask for it ONLY for these types.\n"
        f"  - LANGUAGE-INDEPENDENT types (ONE value shared by every language row): {language_independent}.\n"
        "    A change to one of these is the same value on every language row of the\n"
        "    entity; the language is irrelevant to it. NEVER ask which language row\n"
        "    should receive such a change — there is no such choice to make.\n"
        "  - The entity's TITLE is per-language, like a text property.\n"
        "  A sharedId identifies the ENTITY, not a language row: the same sharedId\n"
        "  names every language row of that entity."
    )


__all__ = [
    "LANGUAGE_INDEPENDENT_PROPERTY_TYPES",
    "PER_LANGUAGE_PROPERTY_TYPES",
    "TITLE_IS_PER_LANGUAGE",
    "is_language_independent_property_type",
    "is_per_language_property_type",
    "language_scope_label",
    "language_scope_rule",
]
