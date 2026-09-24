"""Isolated tests for carrying the server-parsed session cookie to the API session.

Documents the "entity writes return 401 while reads look fine" regression: a
cookie re-built with ``cookies.set(domain=hostname)`` comes out of
http.cookiejar as ``domain_specified``, which is never sent to a bare host
like ``localhost`` (the domain would have to end in ``.localhost``). The
server-parsed cookie must travel over verbatim instead.
"""

from http.cookiejar import Cookie

from requests import Request
from requests.cookies import RequestsCookieJar, get_cookie_header, merge_cookies


def _server_parsed_cookie() -> Cookie:
    """What http.cookiejar stores for a host-only ``Set-Cookie`` (no Domain).

    The effective request host ``localhost`` is stored as ``localhost.local``
    with ``domain_specified=False`` — that flag is what lets the header out.
    """
    return Cookie(
        version=0,
        name="connect.sid",
        value="s%3Aabc.def",
        port=None,
        port_specified=False,
        domain="localhost.local",
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


def _cookie_header(jar: RequestsCookieJar) -> str:
    """The Cookie header the jar would put on the wire (pure, no I/O)."""
    return get_cookie_header(jar, Request("GET", "http://localhost/api/stats").prepare()) or ""


def test_merged_login_session_cookie_reaches_the_wire() -> None:
    login_jar = RequestsCookieJar()
    login_jar.set_cookie(_server_parsed_cookie())
    api_jar = RequestsCookieJar()

    merge_cookies(api_jar, login_jar)

    assert _cookie_header(api_jar) == "connect.sid=s%3Aabc.def"


def test_cookie_rebuilt_by_set_never_reaches_the_wire() -> None:
    # The pre-fix mechanism: rebuilding the value by hand with the URL's
    # hostname. This is why the session authenticated nowhere but public
    # (unauthenticated) reads — every write got 401.
    jar = RequestsCookieJar()
    jar.set("connect.sid", "s%3Aabc.def", domain="localhost", path="/")

    assert _cookie_header(jar) == ""
