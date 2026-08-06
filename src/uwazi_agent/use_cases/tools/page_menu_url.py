import re
import unicodedata

from uwazi_agent.domain.agent_page_menu_link import AgentPageMenuLink


_SLUG_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_DASH_RUN_RE = re.compile(r"-{2,}")


def _to_ascii_slug(value: str) -> str:
    """Lower-case, ASCII-only, dash-separated slug.

    Used as a last-resort default when the caller did not pass a ``url`` or a
    ``slug`` for a page menu link. Emojis, accents, tabs and every other
    non-ASCII character are stripped via NFKD decomposition.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    lower = ascii_only.lower()
    dashed = _SLUG_NORMALIZE_RE.sub("-", lower).strip("-")
    return _DASH_RUN_RE.sub("-", dashed)


def _sanitize_url(raw: str) -> str:
    """Return a clean, ASCII-only path that is safe to use as a menu URL.

    The Uwazi settings endpoint accepts a list of ``{title, type, url}``
    entries. The ``url`` is rendered in the navigation, so anything that is
    not a stable ASCII path is a footgun: emojis get percent-encoded, tabs
    sneak in from copy-paste, leading whitespace breaks the route. This
    helper strips all of that and guarantees a single leading ``/``.
    """
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    collapsed = re.sub(r"\s+", "", ascii_only)
    collapsed = collapsed.strip()
    if not collapsed:
        return ""
    if not collapsed.startswith("/"):
        collapsed = "/" + collapsed
    return collapsed


def build_page_menu_url(link: AgentPageMenuLink) -> str:
    """Build the final URL for a :class:`AgentPageMenuLink`.

    Precedence (first non-empty wins):
        1. ``link.url``  — sanitized.
        2. ``/page/{shared_id}/{slug}``  — if both ``shared_id`` and ``slug``
           are set.
        3. ``/page/{shared_id}``  — if only ``shared_id`` is set.
        4. ``/page/{slug}``  — fallback when neither was given (rarely
           useful; the caller should usually pass a ``shared_id``).
    """
    if link.url:
        return _sanitize_url(link.url)
    if link.shared_id:
        slug = _to_ascii_slug(link.slug or "")
        if slug:
            return _sanitize_url(f"/page/{link.shared_id}/{slug}")
        return _sanitize_url(f"/page/{link.shared_id}")
    if link.slug:
        return _sanitize_url(f"/page/{_to_ascii_slug(link.slug)}")
    return ""


def sanitize_menu_title(raw: str) -> str:
    """Trim a menu link title and strip non-printable characters.

    Titles in the Uwazi UI are plain text — keep them human-readable but
    remove tabs, newlines and other invisible characters that often leak in
    from LLM-generated strings.
    """
    if not raw:
        return ""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", raw)
    return cleaned.strip()
