"""The web app's static assets must resolve from the package (offline, per AGENTS.md).

When the single-module ``drivers/web/app.py`` became the ``drivers/web/app/``
package, ``Path(__file__).parent / "static"`` started pointing at a directory
that does not exist. ``mkdir`` then created an EMPTY one, the mount served it,
and every pdf.js request answered ``{"detail":"Not Found"}``. This guards the
path so the review window's document viewer cannot silently 404 again.
"""

from __future__ import annotations

from uwazi_admin_agent.drivers.web.app.state import _STATIC_DIR


def test_static_dir_contains_the_packaged_pdf_viewer() -> None:
    assert (_STATIC_DIR / "pdfjs" / "review_viewer.html").is_file()
    assert (_STATIC_DIR / "pdfjs" / "review_viewer.mjs").is_file()
    assert (_STATIC_DIR / "pdfjs" / "pdf.mjs").is_file()
