"""The ``links`` CLI step: print ready-made admin login links.

Each printed link opens the admin web login page with the Uwazi instance
(and, when ``--user`` is given, the account) already filled in, so the
recipient only types their Uwazi password. The link carries NO credential.

The instance is always a CONFIGURED url matched against ``UWAZI_URL``; a
parameter that names no configured instance is refused (exit code 2) rather
than guessed.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from uwazi_admin_agent.configuration import ROOT_PATH
from uwazi_admin_agent.domain.instance import parse_instance_urls
from uwazi_admin_agent.drivers.links import login_links, match_instance

# ``load_dotenv`` is idempotent; safe to call from every CLI invocation.
_ENV_LOADED: bool = load_dotenv(ROOT_PATH / ".env")


def run_links(base_url: str, user: str, instance: str | None) -> int:
    """Print one login link per selected instance; return the exit code.

    ``instance`` selects ONE configured instance (by url or by short token);
    ``None`` prints a link for every configured instance.
    """
    configured = parse_instance_urls(os.environ.get("UWAZI_URL"))
    if not configured:
        print("No Uwazi instance configured — set UWAZI_URL", file=sys.stderr)
        return 2
    if instance is not None and instance.strip():
        matched, error = match_instance(instance, configured)
        if error is not None or matched is None:
            print(f"No configured instance matches {instance!r}", file=sys.stderr)
            return 2
        instance = matched
    for link in login_links(configured, base_url, user, instance):
        print(link)
    return 0


__all__ = ["run_links"]
