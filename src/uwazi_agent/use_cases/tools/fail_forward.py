import difflib

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def suggest_template_names(deps: UwaziAgentToolsDependencies, bad_name: str) -> str:
    available = await deps.template_api.get_template_names()
    close = difflib.get_close_matches(bad_name, available, n=5, cutoff=0.4)
    if not close:
        close_i = difflib.get_close_matches(bad_name.lower(), [n.lower() for n in available], n=5, cutoff=0.4)
        idx_map = {n.lower(): n for n in available}
        close = [idx_map[n] for n in close_i]
    if close:
        suggestions = ", ".join(f"'{n}'" for n in close)
        return f"Error: Template '{bad_name}' not found. Did you mean {suggestions}? Available templates: {available}. Please retry with the correct name."
    return f"Error: Template '{bad_name}' not found. Available templates: {available}. Please retry with one of these names."


async def suggest_thesauri_names(deps: UwaziAgentToolsDependencies, bad_name: str, language: str = "en") -> str:
    thesauri = await deps.thesauri_api.get_thesauris(language=language)
    available = [t.name for t in thesauri]
    close = difflib.get_close_matches(bad_name, available, n=5, cutoff=0.4)
    if not close:
        close_i = difflib.get_close_matches(bad_name.lower(), [n.lower() for n in available], n=5, cutoff=0.4)
        idx_map = {n.lower(): n for n in available}
        close = [idx_map[n] for n in close_i]
    if close:
        suggestions = ", ".join(f"'{n}'" for n in close)
        return f"Error: Thesaurus '{bad_name}' not found. Did you mean {suggestions}? Available thesauri: {available}. Please retry with the correct name."
    return f"Error: Thesaurus '{bad_name}' not found. Available thesauri: {available}. Please retry with one of these names."
