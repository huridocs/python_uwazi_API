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


INSTANCE_KEY: str = instance_key(os.environ["UWAZI_URL"])

# The template whose PDFs are filled, and the property to fill, configured via
# ``.env`` so operators do not hard-code them in the UI.
TEMPLATE_NAME: str = os.environ.get("PROPERTY_FILLER_TEMPLATE", "DOCUMENT")
PROPERTY_NAME: str = os.environ.get("PROPERTY_FILLER_PROPERTY", "topic")
FILTER_PROPERTY: str = os.environ.get("PROPERTY_FILLER_FILTER", "document_type")

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
