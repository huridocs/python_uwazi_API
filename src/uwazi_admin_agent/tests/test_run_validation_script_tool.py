"""Isolated unit tests for the ``run_validation_script`` tool's report formatter.

Per ``AGENTS.md``: no mocks/stubs, no network. ``_format_result`` is a pure
function of a ``ValidationResult`` -\u003e str. These tests build literal
``ValidationResult`` / ``EntityDiff`` values (plain objects, literal data) and
assert the no-op warning is surfaced iff the run PASSED with zero changed diffs.
The gate's ``passed`` semantics are unchanged (``ran_clean AND restore_equal``);
the warning only nudges the LLM, it does not flip the pass.
"""

from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_agent.ports.template_api_port import TemplateApiPort

from uwazi_admin_agent.domain.validation_result import EntityDiff, ValidationResult
from uwazi_admin_agent.use_cases.run_validation_script_tool import _format_result, run_validation_script
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps


def _raw(title: str) -> dict:
    return {"_id": "x", "sharedId": "S1", "title": title, "language": "en"}


def test_passed_with_zero_diffs_warns_noop() -> None:
    before = _raw("A")
    result = ValidationResult(
        passed=True,
        script_result="merged 0 entities",
        diffs=[EntityDiff(shared_id="S1", before=before, after=before)],
        restore_equal=True,
    )

    report = _format_result(result, attempt=1, limit=3)

    assert "# Validation attempt 1/3: PASSED" in report
    assert "## No-op warning" in report
    assert "0 diffs" in report
    assert "query_entities" in report


def test_passed_with_changes_does_not_warn_noop() -> None:
    result = ValidationResult(
        passed=True,
        script_result="merged 2 entities",
        diffs=[
            EntityDiff(shared_id="S1", before=_raw("A"), after=_raw("A-merged")),
            EntityDiff(shared_id="S2", before=_raw("B"), after=None),  # deleted
        ],
        restore_equal=True,
    )

    report = _format_result(result, attempt=1, limit=3)

    assert "## No-op warning" not in report


def test_failed_with_zero_diffs_does_not_warn_noop() -> None:
    """The no-op warning is a PASS-only nudge; a FAILED run already has its own
    diagnostic (script error / restore mismatch), so it must not double-warn."""
    result = ValidationResult(
        passed=False,
        script_error="TypeError: 'AgentEntitySummary' object is not subscriptable",
        diffs=[EntityDiff(shared_id="S1", before=_raw("A"), after=_raw("A"))],
        restore_equal=True,
    )

    report = _format_result(result, attempt=1, limit=3)

    assert "## No-op warning" not in report
    assert "## Script error" in report


def test_passed_noop_warning_still_reports_pass_footer() -> None:
    """The warning must not flip the verdict: the run still PASSED (reversibility
    holds), so the PASS footer is emitted alongside the no-op warning."""
    before = _raw("A")
    result = ValidationResult(
        passed=True,
        script_result="nothing to do",
        diffs=[EntityDiff(shared_id="S1", before=before, after=before)],
        restore_equal=True,
    )

    report = _format_result(result, attempt=1, limit=3)

    assert "## No-op warning" in report
    assert "VALIDATION PASSED" in report


# --- required-property pre-check ----------------------------------------------


import asyncio

from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_agent.ports.template_mapper_port import TemplateMapperPort


class _TemplatePort(TemplateApiPort, ThesauriApiPort, TemplateMapperPort):
    """Real port subclass carrying one literal template set; nothing else."""

    def __init__(self, templates: list[AgentTemplate]) -> None:
        self._templates = templates

    async def get_templates(self) -> list[AgentTemplate]:
        return self._templates

    async def get_templates_by_names(self, names: list[str]) -> list[AgentTemplate]:
        by_name = {t.name: t for t in self._templates}
        return [by_name[n] for n in names if n in by_name]

    async def get_template_names(self) -> list[str]:
        return [t.name for t in self._templates]

    def create_template(self, *a: object, **k: object) -> None: ...

    def delete_template(self, *a: object, **k: object) -> None: ...

    def update_template(self, *a: object, **k: object) -> None: ...

    async def get_thesauris(self, language: str) -> list:
        return []

    async def get_thesauris_by_names(self, names: list[str], language: str) -> list:
        return []

    async def create_thesauri(self, *a: object, **k: object) -> dict:
        return {}

    async def update_thesauri(self, *a: object, **k: object) -> dict:
        return {}

    async def delete_thesauri(self, name: str, language: str) -> dict:
        return {}

    def to_agent(self, *a: object, **k: object) -> None:
        return None

    def to_api(self, *a: object, **k: object) -> None:
        return None


class _LoudEntityApi(EntityApiPort):
    """Every method fails loudly: the pre-check must never touch the entity API."""

    async def create_entities(self, entities: list, language: str) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")

    async def get_entities_by_shared_ids(self, *a: object, **k: object) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")

    async def search_entities_by_text(self, *a: object, **k: object) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")

    async def get_entities_by_template(self, *a: object, **k: object) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")

    async def search_entities_by_filter(self, *a: object, **k: object) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")

    async def update_entities(self, updates: list, language: str) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")

    async def delete_entities_by_shared_ids(self, shared_ids: list[str]) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")

    async def set_entities_publish_status(self, *a: object, **k: object) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")

    async def get_publish_status(self, shared_ids: list[str], language: str) -> list:
        raise AssertionError("entity_api must not be touched by the pre-check")


class _LoudEntityRepository(EntityRepositoryPort):
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict:
        raise AssertionError("entity_repository must not be touched by the pre-check")

    async def get_raw_by_internal_id(self, internal_id: str) -> dict:
        raise AssertionError("entity_repository must not be touched by the pre-check")

    async def save_raw(self, raw: dict) -> None:
        raise AssertionError("entity_repository must not be touched by the pre-check")

    async def create_raw(self, raw: dict) -> str:
        raise AssertionError("entity_repository must not be touched by the pre-check")

    async def delete_by_shared_id(self, shared_id: str) -> None:
        raise AssertionError("entity_repository must not be touched by the pre-check")


class _CtxStub:
    """Minimal stand-in for the pydantic-ai RunContext: only ``deps`` is read."""

    def __init__(self, deps: AdminAgentDeps) -> None:
        self.deps = deps


def _template(name: str, required_props: list[tuple[str, str]]) -> AgentTemplate:
    return AgentTemplate(
        name=name,
        properties=[AgentProperty(name=prop_name, type=prop_type, required=True) for prop_name, prop_type in required_props],
    )


def _dummy_spec(template_name: str, metadata: dict) -> list[AgentEntityCreate]:
    return [AgentEntityCreate(title="Dummy A", template_name=template_name, metadata=metadata)]


def _deps(template_api: _TemplatePort) -> AdminAgentDeps:
    return AdminAgentDeps(
        thesauri_api=template_api,
        template_api=template_api,
        template_mapper=template_api,
        entity_api=_LoudEntityApi(),
        entity_repository=_LoudEntityRepository(),
    )


def test_validation_rejects_missing_required_property() -> None:
    """A dummy_spec missing a required property is rejected BEFORE the harness
    runs, without consuming a validation attempt."""
    deps = _deps(_TemplatePort([_template("Report", [("document_type", "text")])]))
    deps.validation_attempts = 0
    deps.validation_limit = 3
    spec = _dummy_spec("Report", metadata={"summary": "x"})

    out = asyncio.run(run_validation_script(_CtxStub(deps), "pass", spec))

    assert "# VALIDATION REJECTED" in out
    assert "required property 'document_type'" in out
    assert "type text" in out
    assert "did NOT consume" in out
    assert deps.validation_attempts == 0


def test_validation_required_check_passes_when_required_present() -> None:
    """A spec carrying every required property is not rejected by the pre-check
    (it proceeds toward the harness, which the loud ports catch)."""
    deps = _deps(_TemplatePort([_template("Report", [("document_type", "text")])]))
    spec = _dummy_spec("Report", metadata={"document_type": "report"})

    out = asyncio.run(run_validation_script(_CtxStub(deps), "pass", spec))

    # The pre-check passed; the run proceeded past it (loud ports fail later).
    assert "VALIDATION REJECTED" not in out


def test_validation_required_check_degrades_on_unknown_template() -> None:
    """An unknown template name can't carry required info: the pre-check must
    not reject; the run proceeds (loud ports catch it later)."""
    deps = _deps(_TemplatePort([]))
    spec = _dummy_spec("Unknown", metadata={"summary": "x"})

    out = asyncio.run(run_validation_script(_CtxStub(deps), "pass", spec))

    assert "VALIDATION REJECTED" not in out
