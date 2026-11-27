"""The header's instance badge: which Uwazi each open window is pointed at.

Every page looks the same whatever instance it serves — same wordmark, same
tables, same tabs. Two windows on two instances are then indistinguishable, and
the operator cannot tell which Uwazi a Run is about to write to. The target URL
is that one fact, so it is rendered in the top bar of every logged-in page (the
login and guidance pages serve no instance, so they show nothing).
"""

from __future__ import annotations

from nicegui import ui

from uwazi_admin_agent.drivers.web.session import page_instance_url


def instance_target(instance_url: str | None = None) -> str:
    """The Uwazi URL a page targets, as the header shows it (pure).

    An explicit ``instance_url`` — the instance a page captured at build time —
    wins; ``None`` reads the session-bound instance. The value is trimmed so a
    trailing space in ``UWAZI_URL`` never leaks into the label, and an empty
    result means no instance is served: the caller renders nothing.
    """
    target = page_instance_url() if instance_url is None else instance_url
    return str(target or "").strip()


def instance_badge(instance_url: str | None = None) -> None:
    """Render the targeted Uwazi URL in the page's top bar.

    It is a link: opening the instance is what the old "Open Uwazi" tab did, and
    putting the destination on screen removes the guesswork about which one it
    is. Nothing is rendered when the page serves no instance.
    """
    target = instance_target(instance_url)
    if not target:
        return
    with ui.row().classes("header-instance items-center no-wrap gap-1"):
        ui.icon("dns").classes("header-instance-icon")
        ui.link(target, target, new_tab=True).classes("header-instance-url")


__all__ = ["instance_badge", "instance_target"]
