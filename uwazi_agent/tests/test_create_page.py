"""Unit tests for the unified page ``create_page`` tool.

These tests stub out the page API and (where relevant) the settings
API, and assert the tool's plumbing:

* input validation (blocks xor content; both forbidden; neither forbidden);
* block-template rendering + happy path against the page API;
* custom-HTML/JS path against the page API;
* automatic Settings → Links menu-link append on success;
* best-effort menu-link failure does NOT roll back the create;
* idempotency: a literal duplicate call in the same session is rejected;
* graceful error paths when ``page_api`` or ``settings_api`` is missing.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from uwazi_agent.domain.agent_page_mutation_result import AgentPageMutationResult
from uwazi_agent.use_cases.tools.create_page import create_page
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.menu_link import MenuLink


class _MockRunContext:
    def __init__(self, deps):
        self.deps = deps


def _make_deps(*, page_api=None, settings_api=None, page_builder_dir=None):
    return UwaziAgentToolsDependencies.model_construct(
        thesauri_api=MagicMock(),
        template_api=MagicMock(),
        template_mapper=MagicMock(),
        page_api=page_api,
        settings_api=settings_api,
        page_builder_dir=page_builder_dir,
    )


def _run(awaitable):
    return asyncio.run(awaitable)


def _stub_success_page_api(shared_id: str = "abc123", url: str = "/en/page/abc123"):
    api = AsyncMock()
    api.create_pages = AsyncMock(
        return_value=[AgentPageMutationResult(shared_id=shared_id, success=True, url=url)]
    )
    return api


def _stub_failure_page_api(error: str = "uwazi said no"):
    api = AsyncMock()
    api.create_pages = AsyncMock(
        return_value=[AgentPageMutationResult(shared_id="", success=False, error=error)]
    )
    return api


def _stub_settings_api(existing: list[MenuLink] | None = None):
    api = AsyncMock()
    api.get_menu_links = AsyncMock(return_value=list(existing or []))
    api.set_menu_links = AsyncMock(return_value=[])
    return api


def _default_block_builder_dir():
    """Return the on-disk page-builder dir, for tests that actually render."""
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "drivers" / "page_builder"


class TestCreatePageInputValidation:
    def test_returns_error_when_both_blocks_and_content_provided(self):
        page_api = _stub_success_page_api()
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))
        result = _run(
            create_page(
                ctx,
                title="t",
                blocks=[{"type": "hero", "slots": {"title": "x"}}],
                content="# markdown",
            )
        )
        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "not both" in result
        page_api.create_pages.assert_not_called()

    def test_returns_error_when_neither_blocks_nor_content_provided(self):
        page_api = _stub_success_page_api()
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))
        result = _run(create_page(ctx, title="t"))
        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "one of" in result
        page_api.create_pages.assert_not_called()

    def test_returns_error_when_page_api_missing(self):
        deps = _make_deps(page_api=None, settings_api=_stub_settings_api())
        ctx = _MockRunContext(deps)
        result = _run(create_page(ctx, title="t", content="x"))
        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "page_api" in result


class TestCreatePageBlockTemplatePath:
    def test_renders_blocks_and_creates_page(self):
        page_api = _stub_success_page_api(shared_id="pg1", url="/en/page/pg1")
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(
            _make_deps(
                page_api=page_api,
                settings_api=settings_api,
                page_builder_dir=_default_block_builder_dir(),
            )
        )
        result = _run(
            create_page(
                ctx,
                title="Welcome",
                blocks=[{"type": "hero", "slots": {"title": "Hi"}}],
                vibe="minimal",
                menu_title="Welcome",
            )
        )
        assert isinstance(result, AgentPageMutationResult)
        assert result.shared_id == "pg1"
        assert result.success is True
        page_api.create_pages.assert_called_once()
        sent_page = page_api.create_pages.call_args.kwargs.get("pages") or page_api.create_pages.call_args.args[0]
        assert sent_page[0].title == "Welcome"
        assert "<!DOCTYPE html>" in sent_page[0].content

    def test_block_validation_error_returns_error_string(self):
        page_api = _stub_success_page_api()
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(
            _make_deps(
                page_api=page_api,
                settings_api=settings_api,
                page_builder_dir=_default_block_builder_dir(),
            )
        )
        result = _run(
            create_page(
                ctx,
                title="t",
                blocks=[{"type": "hero", "slots": {}}],  # missing required `title`
            )
        )
        assert isinstance(result, str)
        assert result.startswith("Error rendering page")
        page_api.create_pages.assert_not_called()

    def test_returns_error_when_page_builder_dir_missing_for_blocks(self):
        page_api = _stub_success_page_api()
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(
            _make_deps(page_api=page_api, settings_api=settings_api, page_builder_dir=None)
        )
        result = _run(
            create_page(
                ctx,
                title="t",
                blocks=[{"type": "hero", "slots": {"title": "x"}}],
            )
        )
        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "page_builder_dir" in result
        page_api.create_pages.assert_not_called()


class TestCreatePageCustomHtmlPath:
    def test_passes_content_through_unchanged(self):
        page_api = _stub_success_page_api(shared_id="pg2", url="/en/page/pg2")
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))
        result = _run(
            create_page(
                ctx,
                title="Custom",
                content="# custom markdown",
                javascript="console.log('hi')",
            )
        )
        assert isinstance(result, AgentPageMutationResult)
        assert result.shared_id == "pg2"
        page_api.create_pages.assert_called_once()
        sent_page = page_api.create_pages.call_args.kwargs.get("pages") or page_api.create_pages.call_args.args[0]
        assert sent_page[0].content == "# custom markdown"
        assert sent_page[0].javascript == "console.log('hi')"


class TestCreatePageMenuLink:
    def test_appends_to_existing_menu_links(self):
        page_api = _stub_success_page_api(shared_id="pg3", url="/en/page/pg3")
        settings_api = _stub_settings_api(
            existing=[MenuLink(title="existing", type="link", url="/page/old")]
        )
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))

        result = _run(
            create_page(
                ctx,
                title="New Page",
                content="x",
                menu_title="New Menu Label",
            )
        )
        assert isinstance(result, AgentPageMutationResult)
        assert result.shared_id == "pg3"

        settings_api.get_menu_links.assert_awaited_once()
        args, _ = settings_api.set_menu_links.call_args
        written: list[MenuLink] = args[0]
        assert [link.title for link in written] == ["existing", "New Menu Label"]
        assert written[1].url == "/page/pg3"
        assert written[1].type == "link"

    def test_uses_title_as_menu_label_when_menu_title_omitted(self):
        page_api = _stub_success_page_api(shared_id="pg4")
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))

        _run(create_page(ctx, title="My Title", content="x"))

        args, _ = settings_api.set_menu_links.call_args
        written: list[MenuLink] = args[0]
        assert written[0].title == "My Title"
        assert written[0].url == "/page/pg4"

    def test_menu_link_failure_does_not_roll_back_create(self):
        page_api = _stub_success_page_api(shared_id="pg5")
        settings_api = AsyncMock()
        settings_api.get_menu_links = AsyncMock(side_effect=RuntimeError("uwazi menus down"))
        settings_api.set_menu_links = AsyncMock(return_value=[])
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))

        result = _run(create_page(ctx, title="t", content="x"))
        # The page was created; the tool returns the result, not an error.
        assert isinstance(result, AgentPageMutationResult)
        assert result.shared_id == "pg5"

    def test_menu_step_skipped_when_settings_api_missing(self):
        page_api = _stub_success_page_api(shared_id="pg6")
        deps = _make_deps(page_api=page_api, settings_api=None)
        ctx = _MockRunContext(deps)
        result = _run(create_page(ctx, title="t", content="x"))
        assert isinstance(result, AgentPageMutationResult)
        assert result.shared_id == "pg6"


class TestCreatePageFailureModes:
    def test_failure_result_returns_error_string(self):
        page_api = _stub_failure_page_api("validation failed upstream")
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))
        result = _run(create_page(ctx, title="t", content="x"))
        assert isinstance(result, str)
        assert result.startswith("Error creating page")
        assert "validation failed upstream" in result
        # Menu link must NOT be added when the create itself failed.
        settings_api.set_menu_links.assert_not_called()

    def test_empty_results_returns_error(self):
        page_api = AsyncMock()
        page_api.create_pages = AsyncMock(return_value=[])
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))
        result = _run(create_page(ctx, title="t", content="x"))
        assert isinstance(result, str)
        assert "no result" in result


class TestCreatePageIdempotency:
    def test_duplicate_call_in_same_session_is_rejected(self):
        page_api = _stub_success_page_api(shared_id="pg7")
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))

        first = _run(create_page(ctx, title="Same", content="same body"))
        assert isinstance(first, AgentPageMutationResult)
        assert first.shared_id == "pg7"

        # Second call with the same inputs in the same session -> Error.
        second = _run(create_page(ctx, title="Same", content="same body"))
        assert isinstance(second, str)
        assert second.startswith("Error")
        assert "pg7" in second
        # The page_api.create_pages call count must remain 1.
        assert page_api.create_pages.await_count == 1

    def test_changing_inputs_does_not_collide(self):
        page_api = _stub_success_page_api(shared_id="pg8")
        settings_api = _stub_settings_api()
        ctx = _MockRunContext(_make_deps(page_api=page_api, settings_api=settings_api))

        first = _run(create_page(ctx, title="A", content="body A"))
        assert isinstance(first, AgentPageMutationResult)
        # Different title -> no collision.
        second = _run(create_page(ctx, title="B", content="body A"))
        assert isinstance(second, AgentPageMutationResult)
        assert page_api.create_pages.await_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

