"""Run the admin-agent web app with ``python -m uwazi_admin_agent.drivers.web.app``.

The web app is a package (split out of the original single-module ``app``), so
the ``-m`` form needs this entry point. It keeps the container command and the
console script (``uwazi-admin-web``) equivalent.
"""

from __future__ import annotations

from . import main

if __name__ == "__main__":
    main()
