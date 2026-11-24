"""Web driver module: read the instance data the guided task wizard needs.

The guided wizard asks a fixed set of questions and assembles the prompt
heuristically — no LLM. The only instance data it needs is the template list
(each template's properties and their types) and the configured languages, both
of which Uwazi answers from ``/api/templates`` and ``/api/settings``.

Deliberately builds a bare :class:`UwaziClient` instead of the full runtime: the
runtime also constructs the LLM adapter (which requires an API key), and the
wizard must never fail because of LLM configuration. Templates are REQUIRED (a
failure raises so the UI can degrade to free-text entry); languages are
best-effort, because an unreadable language list only costs the labels.
"""

from __future__ import annotations

from loguru import logger

from uwazi_admin_agent.domain.guided_task import WizardLanguage, WizardProperty, WizardSchema, WizardTemplate
from uwazi_api.client import UwaziClient

# Instance schema per (url, user). Templates/languages change rarely and every
# wizard open would otherwise re-read both endpoints.
_SCHEMA_CACHE: dict[tuple[str, str], WizardSchema] = {}

# Template-only property types: they carry no entity value, so neither reading
# them as input nor writing them as a target makes sense.
_NON_VALUE_PROPERTY_TYPES = frozenset({"preview", "nested"})

# Every Uwazi entity has a title, whatever its template. The API usually
# returns it among ``commonProperties``; when an instance omits that list (or
# the payload shape changes) the pickers would silently lose the one property
# an operator almost always wants to extract or populate. The projection adds
# it back so the wizard always offers it.
_TITLE_PROPERTY = WizardProperty(name="title", label="Title", type="text")


def clear_wizard_schema_cache() -> None:
    """Forget every cached instance schema (tests / after a schema change)."""
    _SCHEMA_CACHE.clear()


def wizard_schema_from_models(templates: object, languages: object) -> WizardSchema:
    """Project the Uwazi models into the wizard's schema (pure-ish projection).

    Split from the HTTP read so the projection is exercised with real model
    objects and no live instance.
    """
    wizard_templates: list[WizardTemplate] = []
    for template in templates or ():
        properties: list[WizardProperty] = []
        for prop in [*getattr(template, "properties", ()), *getattr(template, "common_properties", ())]:
            name = str(getattr(prop, "name", "") or "").strip()
            if not name:
                continue
            raw_type = getattr(prop, "type", None)
            prop_type = str(getattr(raw_type, "value", None) or raw_type or "").strip().lower()
            if prop_type in _NON_VALUE_PROPERTY_TYPES:
                continue
            properties.append(
                WizardProperty(
                    name=name,
                    label=str(getattr(prop, "label", "") or "").strip(),
                    type=prop_type,
                )
            )
        if not any(prop.name == _TITLE_PROPERTY.name for prop in properties):
            properties.insert(0, _TITLE_PROPERTY)
        wizard_templates.append(WizardTemplate(name=str(getattr(template, "name", "") or ""), properties=tuple(properties)))

    wizard_languages = tuple(
        WizardLanguage(
            key=str(getattr(language, "key", "") or "").strip(),
            label=str(getattr(language, "label", "") or "").strip(),
            default=bool(getattr(language, "default", False)),
        )
        for language in languages or ()
        if str(getattr(language, "key", "") or "").strip()
    )
    return WizardSchema(
        templates=tuple(sorted(wizard_templates, key=lambda template: template.name.casefold())),
        languages=wizard_languages,
    )


def load_wizard_schema(url: str, user: str, password: str, *, refresh: bool = False) -> WizardSchema:
    """Read the instance's templates + configured languages (cached per account).

    Raises when the instance cannot be reached or the templates cannot be read:
    the wizard then offers free-text entry with a warning instead of silently
    presenting an empty template list as if the instance had none.
    """
    key = (url, user)
    if not refresh:
        cached = _SCHEMA_CACHE.get(key)
        if cached is not None:
            return cached

    client = UwaziClient(user=user, password=password, url=url)
    templates = client.templates.get()
    languages = None
    try:
        languages = client.settings.get_languages()
    except Exception as exc:  # noqa: BLE001 — labels only; the questions still work
        logger.warning("could not read the instance's configured languages: {}", exc)

    schema = wizard_schema_from_models(templates, languages)
    _SCHEMA_CACHE[key] = schema
    return schema


__all__ = ["clear_wizard_schema_cache", "load_wizard_schema", "wizard_schema_from_models"]
