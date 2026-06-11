"""Unit tests for the page-menu URL sanitizers used by the page agent.

These pin down the exact behaviour that the agent relies on: no emojis,
no leading tabs, no embedded whitespace, and a leading ``/`` in the
final URL. The expected behaviour matches the contract documented on
``add_page_menu_links``.
"""

from uwazi_agent.domain.agent_page_menu_link import AgentPageMenuLink
from uwazi_agent.use_cases.tools.page_menu_url import (
    build_page_menu_url,
    sanitize_menu_title,
)


class TestBuildPageMenuUrl:
    def test_url_is_returned_as_is_when_clean(self):
        link = AgentPageMenuLink(title="x", url="/page/abc/welcome")
        assert build_page_menu_url(link) == "/page/abc/welcome"

    def test_url_strips_emoji(self):
        link = AgentPageMenuLink(title="x", url="/page/abc/welcome-\U0001f600")
        assert build_page_menu_url(link) == "/page/abc/welcome-"

    def test_url_strips_tab_prefix(self):
        link = AgentPageMenuLink(title="x", url="\t/page/abc/welcome")
        assert build_page_menu_url(link) == "/page/abc/welcome"

    def test_url_strips_internal_whitespace(self):
        link = AgentPageMenuLink(title="x", url="/page/abc  /  welcome")
        assert build_page_menu_url(link) == "/page/abc/welcome"

    def test_url_ensures_leading_slash(self):
        link = AgentPageMenuLink(title="x", url="page/abc")
        assert build_page_menu_url(link) == "/page/abc"

    def test_url_from_shared_id_only(self):
        link = AgentPageMenuLink(title="x", shared_id="abc123")
        assert build_page_menu_url(link) == "/page/abc123"

    def test_url_from_shared_id_and_slug(self):
        link = AgentPageMenuLink(title="x", shared_id="abc123", slug="Welcome!")
        assert build_page_menu_url(link) == "/page/abc123/welcome"

    def test_url_from_slug_only_falls_back_to_page(self):
        link = AgentPageMenuLink(title="x", slug="My Welcome Page")
        assert build_page_menu_url(link) == "/page/my-welcome-page"

    def test_url_with_only_accents_becomes_empty(self):
        link = AgentPageMenuLink(title="x", url="/page/é")
        assert build_page_menu_url(link) == "/page/e"


class TestSanitizeMenuTitle:
    def test_trims_surrounding_whitespace(self):
        assert sanitize_menu_title("  Hello  ") == "Hello"

    def test_strips_control_characters(self):
        assert sanitize_menu_title("Hel\nlo\tWorld") == "Hel lo World"

    def test_keeps_unicode_letters_and_emojis(self):
        assert sanitize_menu_title("Hello \U0001f600") == "Hello \U0001f600"
