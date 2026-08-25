from abc import ABC, abstractmethod


class SearchProbePort(ABC):
    """Minimal read-only seam onto Uwazi's ElasticSearch index (Option A, rev 2).

    The dummy-gate settle polls ES to confirm freshly-created/re-indexed dummies
    are **fresh** (their ES ``editDate`` reflects the latest Mongo write) before
    the cleanup delete runs (see :mod:`uwazi_admin_agent.domain.search_probe` for
    the version-conflict orphan this prevents). Implementations answer one
    question: what is the ES doc's current ``editDate`` for a ``sharedId``?

    Returns the ``editDate`` (a ms timestamp) once the doc is visible, or ``None``
    while it is still un-refreshed (or on any probe error — the settle keeps
    polling to the deadline rather than crashing the gate).

    Async by signature (the port is async, matching :class:`EntityRepositoryPort`
    and :class:`FileRepositoryPort`); the underlying ``requests`` call is
    synchronous. No ``uwazi_api`` change — the adapter reaches into
    ``UwaziClient.http`` like the other admin-agent repositories.
    """

    @abstractmethod
    async def shared_id_edit_date(self, shared_id: str, language: str | None = None) -> int | None:
        """Return the ES doc's ``editDate`` for ``filter[sharedId]=shared_id``, or ``None``.

        ``None`` means "not visible yet / not fresh / probe error" — the settle
        treats it as "keep polling to the deadline."
        """
        ...
