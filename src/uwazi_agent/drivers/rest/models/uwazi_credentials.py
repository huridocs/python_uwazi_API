from pydantic import BaseModel


class UwaziCredentials(BaseModel):
    url: str
    username: str
    password: str

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
    """
    raw = (url or "").strip()
    if raw.startswith("https://") or not raw:
        return [raw]
    bare = raw.removeprefix("http://")
    return [f"https://{bare}", f"http://{bare}"]
