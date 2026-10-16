from abc import ABC, abstractmethod


class TranslationPort(ABC):
    """Access to Uwazi's ``POST /api/translationService`` (§ translations).

    A thin seam over ``uwazi_api``'s :class:`TranslationRepository` so a
    generated bulk-edit script can translate metadata text through the SAME
    endpoint Uwazi's own translation flow uses. Async by signature (matching
    the other admin-agent ports); the underlying ``requests`` calls are
    synchronous. No ``uwazi_api`` change: adapters import and delegate.
    """

    @abstractmethod
    async def translate(self, text: str, language_from: str, language_to: str) -> str:
        """Translate one text and return the translated string.

        Raises :class:`~uwazi_api.domain.exceptions.TranslationError` on any
        failure (including :class:`~uwazi_api.domain.exceptions.TranslationNotAvailableError`
        when the tenant has no external translation service configured).
        """
        ...
