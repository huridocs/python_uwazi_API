import json
from typing import Iterable

from requests import Response

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.domain.exceptions import TranslationError, TranslationNotAvailableError
from uwazi_api.domain.translation import Translation, TranslationRequest

# Uwazi proxies the request to the tenant's external translation service, which
# upstream may take a while on long texts; the shared session default read
# timeout (30s in ``request_retry.DEFAULT_TIMEOUT``) cuts those off mid-flight.
# The connect budget is raised too: the translation service often lives on
# another host, so a 5s connect window is tighter than it is for local calls.
TRANSLATION_TIMEOUT: tuple[float, float] = (10.0, 60.0)


class TranslationRepository:
    """Translate text through Uwazi's ``POST /api/translationService``.

    The endpoint is a thin proxy: Uwazi validates the request, forwards it to
    the tenant's translation service and answers with
    ``{"translated_text": "..."}`` (it never echoes the source/target
    languages back).

    Requires an authenticated client (admin/editor/collaborator) *and* the
    ``translationService`` feature flag enabled on the tenant; otherwise Uwazi
    answers 403 with ``{"error": "Feature not available"}``.
    """

    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client

    def translate(self, text: str, language_from: str, language_to: str) -> Translation:
        """Translate one text, raising ``TranslationError`` on any failure."""
        request = TranslationRequest(text=text, language_from=language_from, language_to=language_to)

        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/translationService",
            headers=self.http.headers,
            cookies={},
            data=json.dumps(request.to_payload()),
            timeout=TRANSLATION_TIMEOUT,
        )

        if response.status_code != 200:
            raise self._error(response)

        return Translation.from_response(
            self._decode(response),
            language_from=request.language_from,
            language_to=request.language_to,
        )

    def translate_to_many(self, text: str, language_from: str, languages_to: Iterable[str]) -> list[Translation]:
        """Translate one text into several target languages.

        Requests run strictly serially: Uwazi rate-limits (429) bursts on this
        endpoint, and the React client caps its own concurrency at 2 for the
        same reason. A failing language aborts the batch, so a partial result
        is never mistaken for a complete one.
        """
        results: list[Translation] = []
        for language_to in languages_to:
            results.append(self.translate(text, language_from, language_to))
        return results

    @staticmethod
    def _decode(response: Response) -> object:
        try:
            return json.loads(response.content)
        except ValueError:
            return None

    def _error(self, response: Response) -> TranslationError:
        status = response.status_code
        body = response.content.decode("utf-8", errors="replace")
        if status in (401, 403, 404):
            message = (
                f"Error ({status}) translating text: {body} "
                "The translation endpoint needs authenticated credentials (admin/editor/collaborator) "
                "and the 'translationService' feature flag enabled on this Uwazi instance."
            )
            self.http.graylog.info(message)
            return TranslationNotAvailableError(message)
        message = f"Error ({status}) translating text: {body}"
        self.http.graylog.info(message)
        return TranslationError(message)
