"""CTM API client.

Credentials are read in-process from a dotenv file and never printed. Every
request goes through ``GuardedSession.send``, the last stop before the network:
one origin only, reads always, and a POST only to ``.../accounts/<id>/sms`` and
only when the caller armed it (``allow_post``). No PUT, PATCH or DELETE, ever.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

BASE_URL = "https://api.calltrackingmetrics.com"
DEFAULT_CREDENTIALS = Path.home() / ".config" / "ctm-bot" / "credentials.env"
SECRET_KEYS = ("CTM_ACCESS_KEY", "CTM_SECRET_KEY", "CTM_BASIC_AUTH_TOKEN")
SMS_PATH = re.compile(r"/api/v1/accounts/\d+/sms")
TIMEOUT = 30
MAX_PAGES = 20

# POST .../sms reads `to` and `from`. The server calls the message text `body` internally, but a flat or nested
# `body` is NOT read (probed 2026-10-16: every shape came back "body can't be blank"). CTM's reference is a
# JavaScript app we can't fetch, so the text goes under every plausible name in one request. Whichever the
# server reads wins; narrow this tuple once a confirmed send shows which one it was.
TEXT_KEYS = ("msg", "message", "text", "content")


class CtmError(Exception):
    """Base class for everything this client raises."""


class AuthMissing(CtmError):
    """The credentials file or the keys inside it are missing."""


class Refused(CtmError):
    """A request was refused before it left the machine."""


class ApiError(CtmError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status = status


class SendOutcomeUnknown(CtmError):
    """The POST left the machine but no answer came back: the text may or may not have gone."""


def load_credentials(path: str | os.PathLike | None = None) -> dict[str, str]:
    where = Path(path or os.environ.get("CTM_CREDENTIALS_FILE") or DEFAULT_CREDENTIALS)
    try:
        lines = where.read_text().splitlines()
    except OSError as exc:
        raise AuthMissing(f"cannot read credentials file {where}: {exc.strerror}") from None
    creds: dict[str, str] = {}
    for line in lines:
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        creds[key.strip()] = value.strip().strip("'\"")
    if not (creds.get("CTM_ACCESS_KEY") and creds.get("CTM_SECRET_KEY")) and not creds.get("CTM_BASIC_AUTH_TOKEN"):
        raise AuthMissing(f"{where} needs CTM_ACCESS_KEY and CTM_SECRET_KEY, or CTM_BASIC_AUTH_TOKEN")
    return creds


def scrub(text: str, creds: dict[str, str]) -> str:
    """Blank every secret value out of text before it is shown, logged or raised."""
    for key in SECRET_KEYS:
        value = creds.get(key)
        if value:
            text = text.replace(value, "***")
    return text


def check_request(method: str, url: str, *, allow_post: bool = False) -> None:
    parts = urlsplit(str(url))
    if f"{parts.scheme}://{parts.netloc}".lower() != BASE_URL:
        raise Refused(f"refused: {parts.scheme}://{parts.netloc} is not {BASE_URL}")
    verb = method.upper()
    if verb in ("GET", "HEAD"):
        return
    if verb == "POST" and allow_post and SMS_PATH.fullmatch(parts.path):
        return
    raise Refused(f"refused: {verb} {parts.path} (reads are free, one POST route opens only for a confirmed send)")


class GuardedSession(requests.Session):
    def __init__(self, creds: dict[str, str], *, allow_post: bool = False):
        super().__init__()
        self.allow_post = allow_post
        if creds.get("CTM_ACCESS_KEY") and creds.get("CTM_SECRET_KEY"):
            self.auth = (creds["CTM_ACCESS_KEY"], creds["CTM_SECRET_KEY"])
        else:
            self.headers["Authorization"] = "Basic " + creds["CTM_BASIC_AUTH_TOKEN"]
        self.headers["Accept"] = "application/json"

    def send(self, request, **kwargs):
        check_request(request.method or "", request.url or "", allow_post=self.allow_post)
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = TIMEOUT
        return super().send(request, **kwargs)


def sms_payload(from_number: str, to_number: str, body: str) -> dict[str, str]:
    return {"from": from_number, "to": to_number, **{key: body for key in TEXT_KEYS}}


class Client:
    def __init__(self, creds: dict[str, str] | None = None, *, allow_post: bool = False, session=None):
        self.creds = creds if creds is not None else load_credentials()
        self.session = session or GuardedSession(self.creds, allow_post=allow_post)

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else BASE_URL + path

    def get(self, path: str, **params) -> dict:
        try:
            resp = self.session.get(self._url(path), params=params or None)
        except requests.RequestException as exc:
            raise ApiError(0, scrub(f"{type(exc).__name__}: {exc}", self.creds)) from None
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, scrub(resp.text[:400], self.creds))
        try:
            return resp.json()
        except ValueError:
            raise ApiError(resp.status_code, "response was not JSON") from None

    def _paged(self, path: str, key: str, **params) -> list[dict]:
        rows: list[dict] = []
        data = self.get(path, **params)
        for _ in range(MAX_PAGES):
            rows += data.get(key, [])
            following = data.get("next_page")
            if not following:
                break
            data = self.get(following)
        return rows

    def accounts(self) -> list[dict]:
        return self._paged("/api/v1/accounts.json", "accounts")

    def numbers(self, account: str) -> list[dict]:
        return self._paged(f"/api/v1/accounts/{account}/numbers.json", "numbers")

    def texts(self, account: str) -> list[dict]:
        """Newest page of the account's text history (up to 100 rows)."""
        return self.get(f"/api/v1/accounts/{account}/sms").get("messages", [])

    def post_sms(self, account: str, from_number: str, to_number: str, body: str) -> tuple[int, str]:
        """One POST, no retries. Returns (HTTP status, scrubbed response text). Raises if no answer came back."""
        url = f"{BASE_URL}/api/v1/accounts/{account}/sms"
        try:
            resp = self.session.post(url, json=sms_payload(from_number, to_number, body))
        except requests.RequestException as exc:
            raise SendOutcomeUnknown(scrub(f"{type(exc).__name__}: {exc}", self.creds)) from None
        return resp.status_code, scrub(resp.text[:600], self.creds)
