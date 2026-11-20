"""Web driver module split out of the original ``app`` (behavior preserved)."""

from __future__ import annotations

from nicegui import ui

from uwazi_admin_agent.drivers.links import instance_token, login_link, resolve_login_target

# Import for its side effect: the ``@ui.page("/review/{run_id}")`` route registers
# on import, so the runs table's Review action can navigate to it.
from uwazi_admin_agent.drivers.web import review_page as _review_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web import task_page as _task_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web.session import (
    bind_client_instance,
    clear_client_instance,
    forget_credentials,
    page_instance_url,
    remember_credentials,
)
from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter

from .state import _INSTANCE_URLS


def _login_page(instance_param: str = "", user_param: str = "") -> None:
    """Full-page login gate: validates the Uwazi account before the app loads.

    Nothing of the admin app renders until the credentials pass a real
    ``/api/login`` against the SELECTED Uwazi instance. Successful logins are
    remembered in MEMORY for this session together with the chosen instance
    (only a random, non-secret token is persisted), and the user is sent to
    ``/app``; failures show an inline error and keep the login page up.

    The instance comes from the LINK, never from a picker: the URL's
    ``instance`` parameter is matched against the configured ``UWAZI_URL``
    list and the CONFIGURED url is what is used, so a forged parameter can
    never point the login at another host. The parameter is MANDATORY: a bare
    ``/``, an instance that names no configured instance, or one that names
    several renders a banner and NO form.

    The ``user`` parameter is optional: when the link carries one, the
    username field is not shown at all and that account is used. A link whose
    ``user`` is stale is fixed by dropping ``&user=…`` from the URL — the
    field comes back and the admin types their own name.

    ROLE POLICY: the app authenticates AS the operator's Uwazi account and acts
    with that account's permissions for every read and write. Deploy it with
    admin accounts only (issue invite links with ``&user=<admin>``). The client
    does not enforce a role check because the Uwazi API it uses exposes no
    current-user/role endpoint; the account's own Uwazi authorisation is the
    boundary.
    """
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    # A login client is unregistered: no instance-bound toast or table refresh
    # may reach it, even if this client object was reused after a logout.
    clear_client_instance()
    # The resolved instance is ALWAYS a configured url (or empty) — never the
    # raw parameter — so an unmatched token can never reach the adapter.
    resolved_instance, resolved_user, instance_error = resolve_login_target(instance_param, user_param, _INSTANCE_URLS)
    with ui.column().classes("w-full items-center justify-center min-h-screen"):
        with ui.card().classes("w-full max-w-sm"):
            ui.label("Uwazi Admin Agent").classes("text-h6")
            if instance_error is not None:
                # Nothing to log in with: show the banner and no form at all,
                # so no credential can be typed into a link we cannot honor.
                ui.label(instance_error).classes("text-body1 q-mt-sm")
                ui.label("Ask the person who sent you the link for the correct URL.").classes(
                    "text-body2 text-grey-7 q-mt-sm"
                )
                return
            ui.label("Log in with the Uwazi account used to administer the instance. ").classes("text-body1 q-mt-sm")
            error_label = ui.label().classes("text-negative text-body2")

            if not _INSTANCE_URLS:
                # Nothing to log in to: say so instead of offering a form that
                # could never validate.
                error_label.set_text("No Uwazi instance configured — set UWAZI_URL in .env")
                return

            # The username field is shown only when the link did not name the
            # account; the instance is decided by the link, never by a picker.
            # A bare "/" never reaches this point: without an instance the
            # page renders the guidance banner above instead of the form.
            user_input = None
            if not resolved_user:
                user_input = ui.input("Username").classes("w-full text-h6")
            password_input = ui.input("Password", password=True).classes("w-full text-h6")

            def _submit() -> None:
                # Never fall back to a default account: an empty submit must not
                # authenticate as ``admin`` on an instance that still has default
                # credentials. Both fields are required.
                typed_user = (user_input.value or "").strip() if user_input is not None else ""
                user = (resolved_user or typed_user).strip()
                password = password_input.value or ""
                if not user or not password:
                    error_label.set_text("Enter the Uwazi username and password.")
                    return
                selected_url = resolved_instance
                try:
                    UwaziApiAdapter(user=user, password=password, url=selected_url)
                except Exception as exc:  # noqa: BLE001 — surface every validation failure
                    error_label.set_text(f"Login failed: {exc}")
                    return
                # Credentials live in MEMORY, keyed by a random token stored in
                # the session; the password is never written to disk.
                remember_credentials(selected_url, user, password)
                bind_client_instance()
                ui.navigate.to("/app")

            password_input.on("keydown.enter", _submit)
            with ui.row().classes("q-mt-lg"):
                ui.button("Log in", color="primary", on_click=_submit)


def _logout() -> None:
    """Drop the session credentials and return to the login page.

    Drops the in-memory vault entry AND the persisted session fields, so every
    tab of this browser session is logged out: an open page's timers then read
    nothing instead of the previous instance's runs.

    The redirect keeps this instance in the URL: a bare "/" only shows the
    "instance is mandatory" guidance, so logging out must land on the login
    page for the instance the operator was using.
    """
    instance = page_instance_url()
    forget_credentials()
    # The client binding is cleared as well so a late broadcast cannot reach the
    # logged-out page.
    clear_client_instance()
    ui.notify("Logged out", type="positive")
    target = login_link("", instance_token(instance, _INSTANCE_URLS), "") if instance else "/"
    ui.navigate.to(target)
