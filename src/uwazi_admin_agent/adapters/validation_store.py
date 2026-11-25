"""Per-run persistence for the validation set and its latest score.

Two small files beside the manifest, matching the repo's YAML/JSON conventions:

- ``validation.yaml`` — the frozen set (entities, measured property, labels);
- ``validation_score.json`` — the latest score for the latest committed script.

Both are optional: a run without ``validation.yaml`` simply has no validation
set. Nothing here reads or writes Uwazi, and nothing touches the suggestion
store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from loguru import logger

from uwazi_admin_agent.domain.validation import ValidationScore, ValidationSet


class ValidationStore:
    """Filesystem store for one run's validation set and score."""

    def __init__(self, run_path: Path) -> None:
        self._run_path: Path = Path(run_path)

    @property
    def set_path(self) -> Path:
        return self._run_path / "validation.yaml"

    @property
    def score_path(self) -> Path:
        return self._run_path / "validation_score.json"

    def load_set(self) -> ValidationSet | None:
        """The run's validation set, or None when it has none (unreadable ⇒ None)."""
        path = self.set_path
        if not path.is_file():
            return None
        try:
            return ValidationSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except Exception as exc:  # noqa: BLE001 — a broken file must not break the page
            logger.warning("unreadable validation set {}: {}", path, exc)
            return None

    def save_set(self, validation: ValidationSet) -> None:
        self._run_path.mkdir(parents=True, exist_ok=True)
        self.set_path.write_text(
            yaml.safe_dump(validation.model_dump(mode="json"), sort_keys=False),
            encoding="utf-8",
        )

    def load_score(self) -> ValidationScore | None:
        """The latest score, or None when the run was never evaluated."""
        path = self.score_path
        if not path.is_file():
            return None
        try:
            return ValidationScore.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("unreadable validation score {}: {}", path, exc)
            return None

    def save_score(self, score: ValidationScore) -> None:
        self._run_path.mkdir(parents=True, exist_ok=True)
        self.score_path.write_text(score.model_dump_json(indent=2), encoding="utf-8")

    def mark_contaminated(self, validation: ValidationSet) -> ValidationSet:
        """Flag the set as used for a prompt fix (pure write, no Uwazi read)."""
        if validation.contaminated_at is not None:
            return validation
        updated = validation.model_copy(update={"contaminated_at": datetime.now(timezone.utc)})
        self.save_set(updated)
        return updated


def score_is_stale(validation: ValidationSet, score: ValidationScore | None) -> bool:
    """Whether the stored score no longer describes the current set (pure)."""
    return score is None or score.set_version != validation.version


__all__ = ["ValidationStore", "score_is_stale"]
