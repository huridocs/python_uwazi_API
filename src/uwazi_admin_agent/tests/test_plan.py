import pytest
from pydantic import ValidationError

from uwazi_admin_agent.domain.extraction import CssExtraction, RegexExtraction
from uwazi_admin_agent.domain.filter import EntityFilter
from uwazi_admin_agent.domain.ops import (
    ExtractFromSupportingFileOp,
    RestructureLanguagesOp,
    SetPropertyOp,
)
from uwazi_admin_agent.domain.plan import MigrationPlan


def _filter() -> EntityFilter:
    return EntityFilter(template="Court")


# --- valid plans parse --------------------------------------------------------


def test_valid_set_property_plan_parses() -> None:
    plan = MigrationPlan(
        description="set title",
        ops=[SetPropertyOp(filter=_filter(), property_name="title", value="X")],
    )
    assert plan.ops[0].kind == "set_property"
    assert plan.ops[0].allow_overwrite is False


def test_valid_extract_plan_parses() -> None:
    plan = MigrationPlan(
        description="extract",
        ops=[
            ExtractFromSupportingFileOp(
                filter=_filter(),
                property_name="notes",
                extraction=CssExtraction(selector=".abstract"),
            )
        ],
    )
    assert plan.ops[0].kind == "extract_from_supporting_file"


def test_valid_restructure_plan_parses() -> None:
    plan = MigrationPlan(
        description="merge languages",
        ops=[RestructureLanguagesOp(filter=_filter(), grouping_property="legacy_id", primary_language="en")],
    )
    assert plan.ops[0].kind == "restructure_languages"


def test_multi_op_plan_parses() -> None:
    plan = MigrationPlan(
        description="two ops",
        ops=[
            SetPropertyOp(filter=_filter(), property_name="a", value=1),
            SetPropertyOp(filter=EntityFilter(template="Report"), property_name="b", value=2),
        ],
    )
    assert len(plan.ops) == 2


# --- filter invariants --------------------------------------------------------


def test_filter_with_no_criterion_rejected() -> None:
    with pytest.raises(ValidationError):
        EntityFilter()


def test_filter_with_empty_list_only_rejected() -> None:
    with pytest.raises(ValidationError):
        EntityFilter(shared_ids=[])


def test_filter_with_blank_string_rejected() -> None:
    with pytest.raises(ValidationError):
        EntityFilter(template="   ")


# --- extraction invariants ----------------------------------------------------


def test_extraction_unknown_mechanism_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractFromSupportingFileOp.model_validate(
            {
                "filter": {"template": "Document"},
                "property_name": "notes",
                "extraction": {"mechanism": "xpath", "selector": "x"},
            }
        )


def test_extraction_missing_mechanism_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractFromSupportingFileOp.model_validate(
            {
                "filter": {"template": "Document"},
                "property_name": "notes",
                "extraction": {"selector": "x"},
            }
        )


def test_regex_extraction_default_group() -> None:
    spec = RegexExtraction(pattern="q(.+)")
    assert spec.group == 0


# --- discriminator routing ----------------------------------------------------


def test_op_kind_routes_via_discriminator() -> None:
    plan = MigrationPlan.model_validate(
        {
            "description": "d",
            "ops": [
                {"kind": "set_property", "filter": {"template": "Court"}, "property_name": "x", "value": 1},
                {
                    "kind": "extract_from_supporting_file",
                    "filter": {"template": "Court"},
                    "property_name": "y",
                    "extraction": {"mechanism": "regex", "pattern": "q(.+)"},
                },
                {
                    "kind": "restructure_languages",
                    "filter": {"template": "Court"},
                    "grouping_property": "id",
                    "primary_language": "en",
                },
            ],
        }
    )
    assert isinstance(plan.ops[0], SetPropertyOp)
    assert isinstance(plan.ops[1], ExtractFromSupportingFileOp)
    assert isinstance(plan.ops[2], RestructureLanguagesOp)


def test_unknown_op_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        MigrationPlan.model_validate(
            {
                "description": "d",
                "ops": [{"kind": "delete_everything", "filter": {"template": "Court"}}],
            }
        )


# --- plan invariants ----------------------------------------------------------


def test_empty_plan_rejected() -> None:
    with pytest.raises(ValidationError):
        MigrationPlan(description="d", ops=[])


def test_conflicting_same_target_writes_rejected() -> None:
    with pytest.raises(ValidationError):
        MigrationPlan(
            description="d",
            ops=[
                SetPropertyOp(filter=_filter(), property_name="title", value="A"),
                SetPropertyOp(filter=_filter(), property_name="title", value="B", allow_overwrite=True),
            ],
        )


def test_same_property_different_filters_allowed() -> None:
    plan = MigrationPlan(
        description="d",
        ops=[
            SetPropertyOp(filter=EntityFilter(template="Court"), property_name="title", value="A"),
            SetPropertyOp(filter=EntityFilter(template="Report"), property_name="title", value="B"),
        ],
    )
    assert len(plan.ops) == 2


def test_allow_overwrite_defaults_false() -> None:
    op = SetPropertyOp(filter=_filter(), property_name="x", value=1)
    assert op.allow_overwrite is False
