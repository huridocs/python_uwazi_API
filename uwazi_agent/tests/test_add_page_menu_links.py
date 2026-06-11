"""Unit tests for the page-agent ``add_page_menu_links`` tool.

These tests stub out :class:`SettingsApiPort` and assert the tool's
plumbing — read existing links, append new sanitized entries, write the
combined list back. The e2e behaviour against a real Uwazi instance is
covered by ``test_menu_links_adapter_e2e.py``.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from uwazi_agent.domain.agent_page_menu_link import AgentPageMenuLink
from uwazi_agent.use_cases.tools.add_page_menu_links import add_page_menu_links
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.menu_link import MenuLink


class _MockRunContext:
    def __init__(self, deps):
        self.deps = deps


def _make_deps(settings_api):
    return UwaziAgentToolsDependencies.model_construct(
        thesauri_api=MagicMock(),
        template_api=MagicMock(),
        template_mapper=MagicMock(),
        settings_api=settings_api,
    )


def _run(awaitable):
    return asyncio.run(awaitable)


class TestAddPageMenuLinks:
    def test_appends_to_existing_links(self):
        settings_api = AsyncMock()
        settings_api.get_menu_links = AsyncMock(return_value=[MenuLink(title="existing", type="link", url="/page/existing")])
        settings_api.set_menu_links = AsyncMock(return_value=[])
        ctx = _MockRunContext(_make_deps(settings_api))

        result = _run(
            add_page_menu_links(
                ctx,
                links=[AgentPageMenuLink(title="new page", shared_id="kkuafbu3wll", slug="welcome")],
            )
        )

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].title == "new page"
        assert result[0].url == "/page/kkuafbu3wll/welcome"
        assert result[0].success is True

        args, _ = settings_api.set_menu_links.call_args
        written: list[MenuLink] = args[0]
        assert [link.title for link in written] == ["existing", "new page"]
        assert written[1].url == "/page/kkuafbu3wll/welcome"
        assert written[1].type == "link"

    def test_sanitizes_tab_and_emoji_in_url(self):
        settings_api = AsyncMock()
        settings_api.get_menu_links = AsyncMock(return_value=[])
        settings_api.set_menu_links = AsyncMock(return_value=[])
        ctx = _MockRunContext(_make_deps(settings_api))

        result = _run(
            add_page_menu_links(
                ctx,
                links=[AgentPageMenuLink(title="new page", url="\t/page/abc/welcome-\U0001f600")],
            )
        )

        assert isinstance(result, list)
        assert result[0].url == "/page/abc/welcome-"

    def test_returns_error_when_settings_api_missing(self):
        deps = UwaziAgentToolsDependencies.model_construct(
            thesauri_api=MagicMock(),
            template_api=MagicMock(),
            template_mapper=MagicMock(),
            settings_api=None,
        )
        result = _run(add_page_menu_links(_MockRunContext(deps), links=[]))
        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "settings_api" in result

    def test_returns_error_when_entry_has_no_resolvable_url(self):
        settings_api = AsyncMock()
        settings_api.get_menu_links = AsyncMock(return_value=[])
        settings_api.set_menu_links = AsyncMock(return_value=[])
        ctx = _MockRunContext(_make_deps(settings_api))

        result = _run(add_page_menu_links(ctx, links=[AgentPageMenuLink(title="orphan")]))

        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "title" in result
        settings_api.set_menu_links.assert_not_called()

    def test_propagates_unexpected_get_failure(self):
        settings_api = AsyncMock()
        settings_api.get_menu_links = AsyncMock(side_effect=RuntimeError("boom"))
        settings_api.set_menu_links = AsyncMock(return_value=[])
        ctx = _MockRunContext(_make_deps(settings_api))

        with pytest.raises(RuntimeError):
            _run(
                add_page_menu_links(
                    ctx,
                    links=[AgentPageMenuLink(title="x", shared_id="abc")],
                )
            )

    def test_propagates_unexpected_set_failure(self):
        settings_api = AsyncMock()
        settings_api.get_menu_links = AsyncMock(return_value=[])
        settings_api.set_menu_links = AsyncMock(side_effect=RuntimeError("boom"))
        ctx = _MockRunContext(_make_deps(settings_api))

        with pytest.raises(RuntimeError):
            _run(
                add_page_menu_links(
                    ctx,
                    links=[AgentPageMenuLink(title="x", shared_id="abc")],
                )
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
