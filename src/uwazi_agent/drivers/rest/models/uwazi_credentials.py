from pydantic import BaseModel, field_validator, model_validator

from uwazi_api.domain.credentials import MISSING_CREDENTIALS_MESSAGE, has_credentials
from uwazi_api.instance_urls import first_instance_url


class UwaziCredentials(BaseModel):
    url: str
    username: str
    password: str

    @model_validator(mode="after")
    def _both_credentials_required(self) -> "UwaziCredentials":
        """Refuse an empty username or password before any login is attempted.

        No default account is ever assumed: a request with a blank credential
        is rejected here, so nothing reaches the Uwazi instance.
        """
        if not has_credentials(self.username, self.password):
            raise ValueError(MISSING_CREDENTIALS_MESSAGE)
        return self

    @field_validator("url")
    @classmethod
    def _single_instance(cls, value: str) -> str:
        """Collapse a configured LIST to the one instance this request targets.

        The shared ``UWAZI_URL`` format allows a comma-separated list; the REST
        API serves exactly one instance, so its first entry is used — the same
        convention as the CLIs and the property filler.
        """
        return first_instance_url(value) or ""

    def make_url_secure(self):
        if "https://" in self.url:
            return

        self.url = self.url.replace("http://", "")
        self.url = "https://" + self.url


def login_url_candidates(url: str) -> list[str]:
    """Base URLs to try for the login handshake, in order.

    Works for both schemes without guessing:

    - ``https://…`` → only that URL: an explicit https never downgrades.
    - ``http://…`` → ``https://…`` first (the upgrade intent of
      ``make_url_secure``), then the given ``http://…`` so plain-HTTP
      instances (local dev) still work instead of failing through minutes of
      connection retries against a TLS-less port.
    - scheme-less (``localhost:3000``) → both, https first.

    Host, port, path and any ``/{lang}`` suffix are preserved verbatim;
    normalization is ``HttpClientAdapter``'s job. Pure: no I/O.

    ``url`` may arrive as a comma-separated LIST (the shared ``UWAZI_URL``
    format); the handshake needs ONE instance, so its first entry is used.
    """
    raw = (first_instance_url(url) or "").strip()
    if raw.startswith("https://") or not raw:
        return [raw]
    bare = raw.removeprefix("http://")
    return [f"https://{bare}", f"http://{bare}"]
