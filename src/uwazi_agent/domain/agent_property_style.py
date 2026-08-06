from enum import Enum


class AgentPropertyStyle(str, Enum):
    """The ``style`` field on a template property as it appears in the Uwazi UI.

    The Uwazi UI presents three style choices to the template editor for
    ``image`` and ``preview`` properties: ``cover``, ``fill`` and ``fit``.
    This enum mirrors those exact UI labels so the LLM-facing surface
    matches what a human editing the template would see.

    The Uwazi server, however, persists only two values on disk
    (``cover`` and ``contain`` — see ``AbstractImageProperty`` in the
    Uwazi server code). ``fill`` and ``fit`` are UI synonyms:

    * ``fill`` (fills the container, may crop) → stored as ``cover``.
    * ``fit`` (fits inside without cropping) → stored as ``contain``.

    The conversion happens in
    :class:`uwazi_agent.adapters.template_mapper.TemplateMapperAdapter`
    — the agent never sends ``fill`` or ``fit`` over the wire; the
    mapper rewrites them to ``cover`` / ``contain`` before the request
    reaches Uwazi.
    """

    COVER = "cover"
    FILL = "fill"
    FIT = "fit"
