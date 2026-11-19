"""The composed SQLite suggestion store.

The original ``sqlite_suggestion_store`` class was a single 1,300-line class.
It is now assembled from mixins split by concern; the public behavior and
method names are unchanged.
"""

from __future__ import annotations

from .base import BaseMixin
from .drift import DriftMixin
from .passes import PassesMixin
from .reads import ReadsMixin
from .writes import WritesMixin


class SqliteSuggestionStore(BaseMixin, PassesMixin, WritesMixin, ReadsMixin, DriftMixin):
    """SQLite-backed suggestion store (mixins split by concern)."""
