import requests
import urllib3
from requests.adapters import HTTPAdapter


def requests_retry_session(retries=4, backoff_factor=0.5, status_forcelist=(429, 500, 502, 504), session=None):
    session = session or requests.Session()
    retry = urllib3.util.retry.Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
