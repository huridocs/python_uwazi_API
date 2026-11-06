"""Test setup for the property filler unit tests.

``uwazi_property_filler.configuration`` validates its settings at import time
and raises when they are missing — correct for the app, but it means importing
the module in a test fails before a single assertion runs. The defaults below
are only enough to import the module; no test connects to Postgres or to a
Uwazi instance (the suite is pure, per the repo's unit-test policy).

``load_dotenv`` runs inside the configuration module with ``override=True``,
so a real ``.env`` at the repo root wins over these values. That is intended:
a developer's own settings are used when they exist.
"""

from __future__ import annotations

import os

os.environ.setdefault("PROPERTY_FILLER_DATABASE_URL", "postgresql://property_filler:test@localhost:5432/property_filler")
os.environ.setdefault("UWAZI_URL", "http://localhost:3000")
