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
    AgentPropertyType.RELATIONSHIP: "TODO: id of a related entity (not yet supported in agent tools)",
}
