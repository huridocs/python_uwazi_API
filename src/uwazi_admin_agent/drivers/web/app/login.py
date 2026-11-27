"""Web driver module split out of the original ``app`` (behavior preserved)."""

from __future__ import annotations

from loguru import logger
from nicegui import ui

from uwazi_admin_agent.domain.login_guard import GUARD, blocked_message
from uwazi_admin_agent.drivers.links import MISSING_INSTANCE_MESSAGE, instance_token, login_link
from uwazi_admin_agent.drivers.web.instances import INSTANCE_URLS, page_link
from uwazi_admin_agent.drivers.web.session import (
    bind_client_instance,
    clear_client_instance,
    forget_credentials,
    page_instance_url,
    remember_credentials,
    session_is_active,
)
from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_api.domain.credentials import MISSING_CREDENTIALS_MESSAGE, has_credentials
from uwazi_api.domain.exceptions import (
    CredentialsRejectedError,
    TwoFactorRequiredError,
    TwoFactorTokenInvalidError,
)

from ..theme import install_theme


def _guidance_page() -> None:
    """Bare root: no instance is named, so point at the shared link (no form)."""
    install_theme()
    with ui.column().classes("uw-auth-page"):
        with ui.element("div").classes("uw-auth-card"):
            with ui.row().classes("uw-auth-brand"):
                with ui.element("div").classes("brand-mark"):
                    ui.label("u")
                with ui.column().classes("gap-0"):
                    ui.label("Uwazi Admin Agent").classes("brand-name")
                    ui.label("Task runs for one instance").classes("brand-sub")
            ui.label("Pick an instance link").classes("uw-auth-title")
            ui.label(MISSING_INSTANCE_MESSAGE).classes("uw-auth-subtitle")
            ui.label("Ask the person who sent you the link for the correct URL.").classes("uw-card-subtitle q-mt-md")


def _login_page(instance_url: str, user_param: str = "") -> None:
    """Full-page login gate: validates the Uwazi account before the app loads.

    Nothing of the admin app renders until the credentials pass a real
    ``/api/login`` against the SELECTED Uwazi instance. When the instance
    answers ``409`` (an account with 2FA enabled), the form asks for the
    6-digit authenticator code and re-posts the same credentials plus that
    code; ``400`` means the code was wrong or expired and keeps the code step
    up. Each web login performs a REAL ``/api/login`` with
    ``use_cached_session=False``, so the cookie cache warm from an earlier
    login can never stand in for the second factor.

    Successful logins are remembered in MEMORY for this session together with
    the chosen instance (only a random, non-secret token is persisted), and the
    user is sent to ``/app``; failures show an inline error and keep the login
    page up.

    The instance comes from the URL's PATH PREFIX, never from a picker or a
    query parameter: the route registered this page for one configured
    ``UWAZI_URL`` and passes that CONFIGURED url in, so a forged path can never
    point the login at another host.

    The ``user`` parameter is optional: when the link carries one, the
    username field is not shown at all and that account is used. A link whose
    ``user`` is stale is fixed by dropping ``?user=…`` from the URL — the
    field comes back and the admin types their own name.

    ROLE POLICY: the app authenticates AS the operator's Uwazi account and acts
    with that account's permissions for every read and write. Deploy it with
    admin accounts only (open the URL with ``?user=<admin>``). The client
    does not enforce a role check because the Uwazi API it uses exposes no
    current-user/role endpoint; the account's own Uwazi authorisation is the
    boundary.
    """
    install_theme()
    # A login client is unregistered: no instance-bound toast or table refresh
    # may reach it, even if this client object was reused after a logout.
    clear_client_instance()
    if session_is_active(instance_url):
        # Already logged in to this instance: a reload of this URL belongs on the
        # app page, never on the form.
        ui.navigate.to(page_link("/app", instance_url))
        return
    user = (user_param or "").strip()
    with ui.column().classes("uw-auth-page"):
        with ui.element("div").classes("uw-auth-card"):
            with ui.row().classes("uw-auth-brand"):
                with ui.element("div").classes("brand-mark"):
                    ui.label("u")
                with ui.column().classes("gap-0"):
                    ui.label("Uwazi Admin Agent").classes("brand-name")
                    ui.label("Admin sign in").classes("brand-sub")
            ui.label("Sign in").classes("uw-auth-title")
            ui.label("Log in with the Uwazi account used to administer the instance. ").classes("uw-auth-subtitle")
            # Show WHERE the credentials are going, ONCE: the full target url
            # (the exact host the password is sent to) and, when the link named
            # one, the account being used.
            with ui.element("div").classes("uw-instance-box w-full"):
                ui.icon("dns")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label("Signing in to").classes("uw-eyebrow")
                    ui.label(instance_url).classes("uw-instance-url")
                    if user:
                        ui.label(f"as {user}").classes("uw-instance-user")
            error_label = ui.label().classes("uw-auth-error q-mt-sm")

            # The username field is shown only when the link did not name the
            # account; the instance is decided by the URL's path prefix, never
            # by a picker.
            user_input = None
            if not user:
                user_input = ui.input("Username").props("outlined dense").classes("w-full q-mt-md")
            password_input = (
                ui.input("Password", password=True, password_toggle_button=True)
                .props("outlined dense")
                .classes("w-full q-mt-sm")
            )
            # Hidden until the instance answers 409: the code is asked for only
            # when the password was accepted and the account uses 2FA.
            token_input = (
                ui.input("Verification code").props("outlined dense inputmode=numeric maxlength=6").classes("w-full q-mt-sm")
            )
            token_input.set_visibility(False)

            # The two-step state lives in this closure only: never persisted,
            # never logged, and gone as soon as the page is left.
            pending: dict[str, str] = {}

            def _reset_pending() -> None:
                pending.clear()
                token_input.value = ""
                token_input.set_visibility(False)
                back_button.set_visibility(False)
                submit_button.set_text("Log in")
                if user_input is not None:
                    user_input.set_enabled(True)
                password_input.set_enabled(True)
                error_label.set_text("")

            def _record_failure(account: str, *, invalid_code: bool) -> None:
                failures = GUARD.record_failure(instance_url, account)
                logger.warning(
                    "Admin web login failed user={} instance={} failures={} invalid_code={}",
                    account,
                    instance_url,
                    failures,
                    invalid_code,
                )
                if GUARD.is_blocked(instance_url, account):
                    error_label.set_text(blocked_message(account))
                elif invalid_code:
                    error_label.set_text(f"Invalid or expired verification code. {failures} failed attempts.")
                else:
                    error_label.set_text(f"Login failed. {failures} failed attempts.")

            def _attempt(account: str, password: str, token: str | None) -> None:
                try:
                    UwaziApiAdapter(
                        user=account,
                        password=password,
                        url=instance_url,
                        token=token,
                        use_cached_session=False,  # the gate ALWAYS proves the second factor
                    )
                except TwoFactorRequiredError:
                    pending["user"], pending["password"] = account, password
                    if user_input is not None:
                        user_input.set_enabled(False)
                    password_input.set_enabled(False)
                    token_input.set_visibility(True)
                    back_button.set_visibility(True)
                    submit_button.set_text("Verify")
                    error_label.set_text("Enter the 6-digit code from your authenticator app.")
                    token_input.run_method("focus")
                    return
                except TwoFactorTokenInvalidError:
                    _record_failure(account, invalid_code=True)
                    token_input.value = ""
                    return
                except CredentialsRejectedError:
                    if pending:
                        # A 401 on the code step means the account locked or the
                        # password changed: Uwazi's own login form returns to the
                        # password step here too.
                        _reset_pending()
                    _record_failure(account, invalid_code=False)
                    return
                except Exception as exc:  # noqa: BLE001 — surface every validation failure
                    error_label.set_text(f"Login failed: {exc}")
                    return
                # A successful login clears the account's failure count.
                GUARD.record_success(instance_url, account)
                # Credentials live in MEMORY, keyed by a random token stored in
                # the session; the password is never written to disk.
                remember_credentials(instance_url, account, password)
                bind_client_instance()
                # Keep the instance in the URL: a reload whose session lost its
                # in-memory credentials then lands back on THIS instance's
                # login page instead of the dummy page.
                ui.navigate.to(page_link("/app", instance_url))

            def _submit() -> None:
                if pending:
                    # Second step: the password already passed, only the code
                    # travels this time (and is never stored anywhere).
                    account, password = pending["user"], pending["password"]
                    if GUARD.is_blocked(instance_url, account):
                        error_label.set_text(blocked_message(account))
                        return
                    token = "".join(ch for ch in (token_input.value or "") if ch.isdigit())[:6]
                    if len(token) != 6:
                        error_label.set_text("Enter the 6-digit code from your authenticator app.")
                        return
                    _attempt(account, password, token)
                    return

                # Never fall back to a default account: an empty submit must not
                # authenticate as ``admin`` on an instance that still has default
                # credentials. Both fields are required.
                typed_user = (user_input.value or "").strip() if user_input is not None else ""
                account = (user or typed_user).strip()
                password = (password_input.value or "") if password_input is not None else ""
                if not has_credentials(account, password):
                    error_label.set_text(MISSING_CREDENTIALS_MESSAGE)
                    return
                # A locked account is refused WITHOUT contacting the instance:
                # the block must stop the login attempts, not merely hide them.
                if GUARD.is_blocked(instance_url, account):
                    error_label.set_text(blocked_message(account))
                    return
                _attempt(account, password, None)

            submit_button = (
                ui.button("Log in", icon="login", on_click=_submit)
                .props("unelevated no-caps color=primary")
                .classes("w-full q-mt-lg")
            )
            back_button = (
                ui.button("Use a different account", on_click=_reset_pending).props("flat no-caps").classes("w-full")
            )
            back_button.set_visibility(False)
            password_input.on("keydown.enter", _submit)
            token_input.on("keydown.enter", _submit)


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
    target = login_link("", instance_token(instance, INSTANCE_URLS), "") if instance else "/"
    ui.navigate.to(target)
