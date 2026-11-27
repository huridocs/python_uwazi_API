"""Isolated tests for the task settings persisted with a run (per AGENTS.md).

The validation set and the LLM budget live on the manifest at task creation, so
a retry (or a later read) can recover them even when the run never reached a
planning pass. Literal values, real models, no instance and no mocks.
"""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.validation import (
    VALIDATION_CUSTOM,
    ValidationConfig,
    ValidationSet,
    label_key,
)
from uwazi_admin_agent.drivers.web.run_service.validation import (
    validation_config_from_dict,
    validation_config_from_set,
    validation_config_to_dict,
)

NOW = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)


def _config() -> ValidationConfig:
    return ValidationConfig(
        mode=VALIDATION_CUSTOM,
        property_name="states",
        property_type="relationship",
        template_names=["Recommendation"],
        sample_size=12,
        selector_code="result = ['rec-1']",
        selector_explanation="the 2024 recommendations",
        shared_ids=["rec-1", "rec-2"],
        language="en",
        input_properties=["judge"],
    )


def test_a_manifests_recipe_round_trips_through_plain_data() -> None:
    """The manifest stores a dict (no import edge), and a retry reads it back whole."""
    manifest = MigrationManifest(
        run_id="run-1",
        created_at=NOW,
        prompt="p",
        script="",
        status=RunStatus.GENERATION_FAILED,
        llm_budget=LlmBudget(
            max_cost=2.5,
            max_requests=50,
            price=LlmPrice(model="m", input_per_mtok=0.1, output_per_mtok=0.2),
        ),
        validation_config=validation_config_to_dict(_config()),
    )

    assert manifest.llm_budget is not None and manifest.llm_budget.max_cost == 2.5
    restored = validation_config_from_dict(manifest.validation_config)
    assert restored == _config()
    # The sample's MEMBERS travel, so a retry scores the same population.
    assert restored is not None and restored.shared_ids == ["rec-1", "rec-2"]


def test_a_generation_failure_still_records_the_settings() -> None:
    """A failed task must hand its settings on: it never reached a planning pass."""
    manifest = MigrationManifest(
        run_id="run-2",
        created_at=NOW,
        prompt="p",
        script="",
        status=RunStatus.GENERATION_FAILED,
        llm_budget=None,
        validation_config=None,
    )
    assert validation_config_from_dict(manifest.validation_config) is None
    # An unbuildable test set is logged, never surfaced as a run warning: the
    # manifest carries no warnings slot at all any more.
    assert not hasattr(manifest, "warnings")


def test_a_frozen_set_reproduces_its_recipe_for_a_later_read() -> None:
    frozen = ValidationSet(
        run_id="run-1",
        property_name="states",
        property_type="relationship",
        template_names=["Recommendation"],
        shared_ids=["rec-1", "rec-2"],
        labels={label_key("rec-1", "en"): [{"value": "id-1"}]},
        mode=VALIDATION_CUSTOM,
        selector_code="result = ['rec-1', 'rec-2']",
        selector_explanation="the 2024 recommendations",
        created_at=NOW,
    )

    config = validation_config_from_set(frozen)

    assert config is not None
    assert config.property_name == "states"
    assert config.shared_ids == ["rec-1", "rec-2"]
    assert config.selector_code == "result = ['rec-1', 'rec-2']"
    assert config.sample_size == 2


def test_a_broken_recipe_reads_as_no_recipe() -> None:
    assert validation_config_from_dict(None) is None
    assert validation_config_from_dict({}) is None
    assert validation_config_from_dict({"unknown_field": 1}) is None
    assert validation_config_from_dict("not a dict") is None
