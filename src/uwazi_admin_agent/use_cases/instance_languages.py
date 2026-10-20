"""The instance's configured languages, rendered for the LLM prompts.

Both prompt-building passes need the SAME set: the clarification agent uses it
to offer real language choices, and the script generator uses it to avoid
inventing candidate language lists (an LLM script that hardcodes dozens of ISO
codes and loops over them writes rows into languages the instance never
configured). The languages come from Uwazi's settings endpoint through the
wired ``settings_api`` port.

Pure formatting over fetched models; the fetch is best-effort (``None`` means
"could not read"), so a failed settings read degrades the prompt instead of
blocking generation.
"""

from __future__ import annotations

from loguru import logger

from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_api.domain.language import Language


def render_language(language: Language) -> str:
    """Render one configured language as ``- key (label) [default]`` (pure)."""
    rendered = f"- {language.key}"
    if language.label:
        rendered += f" ({language.label})"
    if language.default:
        rendered += " [default]"
    return rendered


def render_languages(languages: list[Language] | None) -> list[str]:
    """Render the configured languages as bullet lines (pure)."""
    if languages is None:
        return ["- (unavailable: the instance's language list could not be read)"]
    return [render_language(language) for language in languages] or ["- (none configured)"]


def render_languages_section(languages: list[Language] | None) -> str:
    """The full ``INSTANCE LANGUAGES`` block both prompts embed (pure)."""
    header = "INSTANCE LANGUAGES (configured language keys — these are the ONLY valid `language` values):"
    return "\n".join([header, *render_languages(languages)])


async def fetch_instance_languages(deps: AdminAgentDeps | None) -> list[Language] | None:
    """Read the instance's configured languages (best-effort, ``None`` on failure)."""
    if deps is None or deps.settings_api is None:
        return None
    try:
        return list(await deps.settings_api.get_languages())
    except Exception as exc:  # noqa: BLE001 — degrade, never block a prompt pass
        logger.warning("instance language fetch degraded: {}", exc)
        return None


def system_prompt_with_languages(base_prompt: str, languages: list[Language] | None) -> str:
    """Append the instance-language contract to a system prompt (pure).

    The generation agent is the one that writes ``language=`` arguments into a
    script, so it needs the strongest wording: the configured keys are the only
    valid values, and a candidate list of ISO codes is explicitly forbidden.
    """
    if languages is None:
        rule = (
            "The instance's language list could not be read. Use ONLY a language key "
            "the operator explicitly names in the prompt. NEVER hardcode or invent a "
            "candidate list of ISO language codes."
        )
    else:
        rule = (
            "A `language=` argument must be one of these keys. The operator may name a "
            'language by its label (e.g. "Spanish"); map it to the matching key above. '
            "NEVER hardcode, guess, or invent candidate language lists (e.g. a "
            '`CANDIDATE_LANGS = ["es", "fr", ...]` array of every ISO code): only '
            "configured keys may be written. If the operator names a language that is "
            "NOT configured on this instance, do not write rows for it — say so in "
            "`result` instead."
        )
    return f"{base_prompt.rstrip()}\n\n{render_languages_section(languages)}\n\n{rule}\n"
