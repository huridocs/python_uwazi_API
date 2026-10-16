"""Isolated unit tests for a language-tagged metadata value on the way in.

Uwazi stores text/richtext PER LANGUAGE, as a list of ``{"value",
"language"}`` entries, and every other property type is language-INDEPENDENT.
A writer that knows the target language therefore tags the text value it sends
(the admin review table does — see
``uwazi_admin_agent.use_cases.apply_suggestions_use_case.stamp_language``), and
the write adapter must treat that tag the way the STORAGE model treats it:

- on ``text``/``markdown`` the tag IS the value's language and survives into the
  payload, so the entry lands in the language the operator approved;
- on every other type the tag is not part of the value: it is stripped before
  coercion, so a tagged select label still resolves through its thesaurus (it used
  to fail with ``TypeError: unhashable type: 'dict'``, since the label lookup
  hashes the value) and a tagged date is still coerced to a timestamp.

No mocks, no HTTP: a literal ``Template`` and a thesaurus cache seeded with real
domain objects, so ``ThesauriRepository.get`` answers from memory.
"""

from uwazi_agent.adapters.uwazi_api.entity_mapper import EntityMapper
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template
from uwazi_api.domain.thesauri import Thesauri
from uwazi_api.domain.thesauri_value import ThesauriValue
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository

THESAURUS_ID = "5bfbb1a0471dd0fc16ada146"


def _mapper(*, with_thesaurus: bool = True) -> EntityMapper:
    repo = ThesauriRepository()
    if with_thesaurus:
        # Seeded, not stubbed: ``get`` finds the language cached and never
        # reaches the HTTP adapter it would otherwise need.
        repo._cache["en"] = [  # noqa: SLF001 — the cache IS the repository's state
            Thesauri(
                id=THESAURUS_ID,
                name="states",
                values=[ThesauriValue(id="open-id", label="Open"), ThesauriValue(id="closed-id", label="Closed")],
            )
        ]
    return EntityMapper(template_repo=None, thesauri_repo=repo)


def _coerce(prop_type: PropertyType, value: object, *, with_thesaurus: bool = True) -> object:
    template = Template(
        name="DOCUMENT",
        properties=[PropertySchema(name="p", label="P", type=prop_type, content=THESAURUS_ID)],
    )
    return _mapper(with_thesaurus=with_thesaurus)._coerce_metadata(metadata={"p": value}, template=template, language="en")


# --- per-language types keep the tag ------------------------------------------


def test_a_tagged_text_value_keeps_its_language() -> None:
    assert _coerce(PropertyType.TEXT, [{"value": "hola", "language": "es"}]) == {"p": [{"value": "hola", "language": "es"}]}


def test_two_tagged_text_entries_stay_two_languages() -> None:
    """The list is the per-language storage: one entry per language, both kept."""
    coerced = _coerce(
        PropertyType.TEXT,
        [{"value": "hola", "language": "es"}, {"value": "hello", "language": "en"}],
    )
    assert coerced == {"p": [{"value": "hola", "language": "es"}, {"value": "hello", "language": "en"}]}


def test_markdown_keeps_its_language_too() -> None:
    assert _coerce(PropertyType.MARKDOWN, [{"value": "# hola", "language": "es"}]) == {
        "p": [{"value": "# hola", "language": "es"}]
    }


def test_an_untagged_text_value_is_wrapped_as_before() -> None:
    assert _coerce(PropertyType.TEXT, "hola") == {"p": [{"value": "hola"}]}


# --- language-independent types drop it ---------------------------------------


def test_a_tagged_select_label_still_resolves_through_its_thesaurus() -> None:
    """The regression: the label lookup hashes the value, so a dict raised TypeError."""
    assert _coerce(PropertyType.SELECT, [{"value": "Open", "language": "es"}]) == {"p": ["open-id"]}


def test_a_tagged_multiselect_resolves_every_label() -> None:
    assert _coerce(PropertyType.MULTI_SELECT, [{"value": "Open", "language": "es"}, {"value": "Closed"}]) == {
        "p": ["open-id", "closed-id"]
    }


def test_a_tagged_date_is_still_coerced_to_a_timestamp() -> None:
    assert _coerce(PropertyType.DATE, [{"value": "2024-05-22", "language": "es"}]) == {"p": [{"value": 1716336000}]}


def test_a_tagged_numeric_drops_the_tag() -> None:
    assert _coerce(PropertyType.NUMERIC, [{"value": 42, "language": "es"}]) == {"p": [{"value": 42}]}


def test_a_tagged_link_is_still_coerced_to_label_and_url() -> None:
    assert _coerce(PropertyType.LINK, [{"value": {"label": "L", "url": "u"}, "language": "es"}]) == {
        "p": [{"value": {"label": "L", "url": "u"}}]
    }


def test_an_untouched_read_shape_survives_the_unwrap() -> None:
    """A relationship's read shape has no ``value`` key: it must not be unwrapped."""
    assert _coerce(PropertyType.RELATIONSHIP, [{"shared_id": "s1", "title": "T"}]) == {"p": [{"value": "s1"}]}


def test_an_untagged_value_is_unchanged_for_every_type() -> None:
    """The unwrap is a no-op on the shapes that already worked."""
    assert _coerce(PropertyType.NUMERIC, 42) == {"p": [{"value": 42}]}
    assert _coerce(PropertyType.DATE, "2024-05-22") == {"p": [{"value": 1716336000}]}
    assert _coerce(PropertyType.SELECT, "Open") == {"p": ["open-id"]}
