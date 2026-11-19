"""The composed revert use case (mixins split by concern)."""

from __future__ import annotations

from .audit import AuditMixin
from .base import BaseMixin
from .entity import EntityMixin
from .files import FilesMixin
from .merge import MergeMixin


class RevertRunUseCase(BaseMixin, MergeMixin, EntityMixin, FilesMixin, AuditMixin):
    """Undo a run's writes in safe order (mixins split by concern)."""
