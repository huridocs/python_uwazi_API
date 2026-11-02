"""Configuration for the Uwazi Property Filler (stdlib only).

Mirrors ``uwazi_admin_agent/configuration.py``'s layout: the package runtime
data lives under the repo-level ``data/`` dir so it is clearly scoped and does
not collide with the other packages. One DB row namespace per Uwazi instance
is keyed by ``INSTANCE_KEY``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_PATH: Path = Path(__file__).parent.parent.parent.resolve()
DATA_DIR: Path = ROOT_PATH / "data"

# ``load_dotenv`` is idempotent and mirrors the admin agent's runtime: the app
# reads UWAZI_URL/UWAZI_USER/UWAZI_PASSWORD from the repo-root ``.env``.
# ``override=True`` makes the ``.env`` file authoritative so a stale
# ``UWAZI_URL`` inherited from a parent process cannot redirect the app.
load_dotenv(ROOT_PATH / ".env", override=True)


def instance_key(url: str) -> str:
    """Stable per-instance namespace key from the Uwazi base URL.

    Trailing slashes are stripped so ``https://x.io`` and ``https://x.io/``
    map to the same key.
    """
    return hashlib.sha1(url.rstrip("/").encode("utf-8")).hexdigest()[:16]


def parse_instance_urls(raw: str | None) -> list[str]:
    """The instance URLs in a configured value, in order, de-duplicated.

    The admin agent accepts ``UWAZI_URL`` as a comma-separated list; the
    property filler administers exactly one instance, so this helper lets it
    pick one without crashing on the shared value. Splits on commas, strips
    whitespace, drops empties, and de-duplicates preserving first occurrence.
    """
    if not raw:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for part in raw.split(","):
        url = part.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def select_instance_url(raw: str | None, override: str | None = None) -> str:
    """The single Uwazi URL this process administers.

    ``PROPERTY_FILLER_UWAZI_URL`` (``override``) wins when set, so the filler
    can target a specific instance while ``UWAZI_URL`` lists several. Otherwise
    the FIRST configured URL is used — the same convention as the admin CLI.
    Raises ``RuntimeError`` when neither yields a URL, or when the override
    itself holds a list.
    """
    explicit = (override or "").strip()
    if explicit:
        if "," in explicit:
            raise RuntimeError(
                "PROPERTY_FILLER_UWAZI_URL holds a list of instances; the property filler "
                "needs a single URL. Set it to one instance."
            )
        return explicit
    urls = parse_instance_urls(raw)
    if not urls:
        raise RuntimeError(
            "UWAZI_URL is not set — point it at the instance to administer, or set "
            "PROPERTY_FILLER_UWAZI_URL to the one instance the filler should use."
        )
    return urls[0]


UWAZI_URL: str = select_instance_url(
    os.environ.get("UWAZI_URL"),
    os.environ.get("PROPERTY_FILLER_UWAZI_URL"),
)

INSTANCE_KEY: str = instance_key(UWAZI_URL)

# The template whose PDFs are filled, and the property to fill, configured via
# ``.env`` so operators do not hard-code them in the UI.
TEMPLATE_NAME: str = os.environ.get("PROPERTY_FILLER_TEMPLATE", "DOCUMENT")
PROPERTY_NAME: str = os.environ.get("PROPERTY_FILLER_PROPERTY", "topic")
FILTER_PROPERTY: str = os.environ.get("PROPERTY_FILLER_FILTER", "document_type")
# Secondary text shown under each document title in the UI (e.g. a reference
# number), also used by the list search. Empty disables the subtitle.
SUBTITLE_PROPERTY: str = os.environ.get("PROPERTY_FILLER_SUBTITLE", "")
# Template whose entities are the possible values of ``PROPERTY_NAME`` when it
# is a relationship property (e.g. TOPIC). Empty falls back to the related
# template configured on the property itself; select-like properties always
# use their thesaurus instead.
PROPERTY_TEMPLATE: str = os.environ.get("PROPERTY_FILLER_PROPERTY_TEMPLATE", "")

# Labeling configuration (the /labels mode). ``LABEL_RELATIONSHIP_TYPE`` is the
# Uwazi relationship type that links a document to a label entity, given by
# NAME or id (resolved via the relationship repository). The assignable label
# values come from ``PROPERTY_TEMPLATE`` (the same value template the fill form
# uses). ``LABEL_WEBHOOK_URLS`` are the endpoints notified when a label is
# created/deleted.
LABEL_RELATIONSHIP_TYPE: str = os.environ.get("PROPERTY_FILLER_RELATIONSHIP_TYPE", "")
LABEL_WEBHOOK_URLS: list[str] = [
    u.strip() for u in os.environ.get("PROPERTY_FILLER_LABEL_WEBHOOK_URLS", "").split(",") if u.strip()
]

_DATABASE_URL = os.environ.get("PROPERTY_FILLER_DATABASE_URL")
if not _DATABASE_URL:
    raise RuntimeError(
        "PROPERTY_FILLER_DATABASE_URL is required "
        "(e.g. postgresql://property_filler:property_filler@localhost:5433/property_filler)"
    )
DATABASE_URL: str = _DATABASE_URL

PDF_CACHE_DIR: Path = DATA_DIR / "pdf_cache"
PDF_CACHE_MAX_BYTES: int = 4 * 1024**3

EXTENSIONS_DIR: Path = DATA_DIR / "extensions"

APP_PORT: int = int(os.environ.get("PROPERTY_FILLER_PORT", "7050"))

DEFAULT_LANGUAGE: str = "en"
