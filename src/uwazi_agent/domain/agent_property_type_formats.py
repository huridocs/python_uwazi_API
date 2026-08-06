from uwazi_agent.domain.agent_property_type import AgentPropertyType


# Format instructions surfaced to the LLM (and rendered as the
# ``format_instructions`` field on every ``AgentProperty`` returned by
# ``get_templates_by_names``). These are the *only* shapes accepted on the
# way in (``create_entities`` / ``update_entities``) and the only shapes
# produced on the way out (every tool that returns entities).
#
# The LLM must NOT re-emit the Uwazi on-disk envelope it sees in
# `uwazi_csv_conventions.md` (e.g. ``{"value": {"label": ..., "lat": ...,
# "lon": ...}}`` for geolocation, or ``{"value": {"label": ...,
# "url": ...}}`` for links). The mapper will reject those unless the
# inner object happens to match one of the LLM-facing shapes below.
#
# Conventions used in the strings below:
#   * <float>           — a number with a decimal point (e.g. 39.4699)
#   * <int>             — a whole number
#   * YYYY-MM-DD        — ISO 8601 calendar date
#   * <url>             — http(s) URL
#   * "label"           — thesaurus label, never a UUID
#
# When a property holds multiple values, wrap the accepted single value
# in a list (e.g. ``[label1, label2]`` for multi-select, ``[from, to]``
# for multi-date, ``[{from, to}, {from, to}]`` for multi-date-range).
AGENT_PROPERTY_TYPE_FORMATS: dict[AgentPropertyType, str] = {
    AgentPropertyType.TEXT: "plain string",
    AgentPropertyType.MARKDOWN: "markdown string",
    AgentPropertyType.NUMERIC: "<int> or <float>",
    AgentPropertyType.DATE: '`"YYYY-MM-DD"` (ISO 8601)',
    AgentPropertyType.DATE_RANGE: '`{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}` or `"YYYY-MM-DD->YYYY-MM-DD"`',
    AgentPropertyType.MULTI_DATE: '`["YYYY-MM-DD"]` or `["YYYY-MM-DD", "YYYY-MM-DD"]` (a list of ISO dates)',
    AgentPropertyType.MULTI_DATE_RANGE: '`[{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}]` (a list of ranges)',
    AgentPropertyType.SELECT: 'one thesaurus label string (e.g. `"Approved"`). Never a UUID.',
    AgentPropertyType.MULTI_SELECT: 'a list of thesaurus label strings (e.g. `["Approved", "Pending"]`). Never UUIDs.',
    AgentPropertyType.LINK: '`{"label": "<text>", "url": "<url>"}` or `"<text>|<url>"`',
    AgentPropertyType.IMAGE: "image URL or file",
    AgentPropertyType.MEDIA: "media URL or file",
    AgentPropertyType.GENERATED_ID: "auto-generated identifier (leave empty to generate)",
    AgentPropertyType.GEO_LOCATION: (
        "one of: `[<float>, <float>]` (e.g. `[39.4699, -0.3763]`), "
        '`{"lat": <float>, "lon": <float>}` '
        '(e.g. `{"lat": 39.4699, "lon": -0.3763}`), '
        'or `"<float>|<float>"` (e.g. `"39.4699|-0.3763"`). '
        "For multiple points, use a list of any of those shapes. "
        "On read, the LLM sees `[lat, lon]` pairs; never echo back the read "
        "shape with extra `label`/`lat`/`lon` keys — the mapper will reject it."
    ),
    AgentPropertyType.RELATIONSHIP: (
        "a list of related entities, identified by their stable ``shared_id`` "
        '(e.g. `["k7d2x9ab1cd"]`). You may also pass the on-read shape '
        '`[{"shared_id": "k7d2x9ab1cd", "title": "The Great Gatsby"}]`; only the '
        "``shared_id`` is used on write (the title is informational). To discover "
        "the ``shared_id`` of the entities you want to link to, search for them "
        "first with the entity search tools. On read you receive "
        '`[{"shared_id": ..., "title": ...}]` for each linked entity.'
    ),
    AgentPropertyType.PREVIEW: (
        "a TEMPLATE-ONLY property that tells Uwazi to render the entity's "
        "PRIMARY document (the file the entity is about) as an image at the "
        "top of the entity view. Uwazi auto-generates the image from the file "
        "itself — the user never uploads or picks a preview image. "
        "``preview`` properties NEVER appear in entity metadata — there is "
        "no per-entity value to read or write. When creating or updating a "
        "template, you may set ``style`` (``'cover'`` (default), ``'fill'``, "
        "or ``'fit'``; same values as for ``image`` properties) and "
        "``full_width`` (bool; full-width layout when true). "
        "Do NOT include a ``preview`` property in any entity payload."
    ),
    AgentPropertyType.NESTED: (
        "a TEMPLATE-ONLY group container that lets a template gather a "
        "repeatable sub-set of OTHER properties under one parent key "
        "(Uwazi 'nested'). The parent property itself carries no direct "
        "value — its child properties do. Nested properties NEVER appear "
        "as their own key in entity metadata: only their child keys do "
        "(e.g. a ``birth`` nested parent with children ``date`` and "
        "``place`` surfaces on the entity as ``date`` / ``place`` keys, "
        "never ``birth``). When creating or updating a template, you may "
        "add a ``nested`` property, but you do NOT need to populate any "
        "value for it on entity CRUD payloads — there is no per-entity "
        "value to read or write for the parent. Treat a ``nested`` property "
        "as a structural grouping for the schema agent; the user will "
        "configure which child properties belong to it through Uwazi's "
        "template editor. Do NOT include a ``nested`` parent in any entity "
        "CRUD payload."
    ),
}
