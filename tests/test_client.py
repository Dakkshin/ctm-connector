import pytest
import requests

from ctm_client import (
    BASE_URL, TEXT_KEYS, ApiError, AuthMissing, Client, GuardedSession, Refused, SendOutcomeUnknown, check_request,
    load_credentials, scrub, sms_payload,
)

SECRET = "s3cr3t-value-123"
CREDS = {"CTM_ACCESS_KEY": "acc-key-456", "CTM_SECRET_KEY": SECRET}


def test_load_credentials_parses_dotenv_styles(tmp_path):
    f = tmp_path / "c.env"
    f.write_text("# note\nexport CTM_ACCESS_KEY='abc'\nCTM_SECRET_KEY=\"def\"\n\nCTM_BASIC_AUTH_TOKEN=ghi\n")
    assert load_credentials(f) == {"CTM_ACCESS_KEY": "abc", "CTM_SECRET_KEY": "def", "CTM_BASIC_AUTH_TOKEN": "ghi"}


def test_load_credentials_errors_never_quote_values(tmp_path):
    f = tmp_path / "c.env"
    f.write_text("CTM_ACCESS_KEY=only-half\n")
    with pytest.raises(AuthMissing) as exc:
        load_credentials(f)
    assert "only-half" not in str(exc.value)
    with pytest.raises(AuthMissing):
        load_credentials(tmp_path / "missing.env")


def test_scrub_blanks_every_secret():
    out = scrub(f"boom {SECRET} and acc-key-456", CREDS)
    assert SECRET not in out and "acc-key-456" not in out and out.count("***") == 2


@pytest.mark.parametrize("method,url", [
    ("GET", f"{BASE_URL}/api/v1/accounts.json"),
    ("HEAD", f"{BASE_URL}/api/v1/accounts/1/sms"),
])
def test_reads_pass(method, url):
    check_request(method, url)


@pytest.mark.parametrize("method,url,allow", [
    ("GET", "https://evil.example/api/v1/accounts.json", False),
    ("GET", "http://api.calltrackingmetrics.com/api/v1/accounts.json", False),
    ("GET", "https://api.calltrackingmetrics.com.evil.example/x", False),
    ("POST", f"{BASE_URL}/api/v1/accounts/1/sms", False),  # not armed
    ("POST", f"{BASE_URL}/api/v1/accounts/1/calls", True),  # armed, wrong route
    ("POST", f"{BASE_URL}/api/v1/accounts/1/sms/extra", True),
    ("POST", f"{BASE_URL}/api/v1/accounts/x/sms", True),
    ("PUT", f"{BASE_URL}/api/v1/accounts/1/sms", True),
    ("PATCH", f"{BASE_URL}/api/v1/accounts/1", True),
    ("DELETE", f"{BASE_URL}/api/v1/accounts/1/sms", True),
])
def test_refusals(method, url, allow):
    with pytest.raises(Refused):
        check_request(method, url, allow_post=allow)


def test_armed_post_to_the_sms_route_passes():
    check_request("POST", f"{BASE_URL}/api/v1/accounts/123456/sms", allow_post=True)


def test_guard_stops_requests_before_the_network():
    session = GuardedSession(CREDS)
    with pytest.raises(Refused):
        session.get("https://evil.example/")
    with pytest.raises(Refused):
        session.post(f"{BASE_URL}/api/v1/accounts/1/sms", json={})  # unarmed


def test_payload_carries_to_from_and_every_text_name_once():
    payload = sms_payload("+18005550100", "+14805550123", "hi")
    assert payload["from"] == "+18005550100" and payload["to"] == "+14805550123"
    assert all(payload[k] == "hi" for k in TEXT_KEYS)
    assert set(payload) == {"from", "to", *TEXT_KEYS}


class FakeResponse:
    def __init__(self, status=200, text="{}", data=None):
        self.status_code, self.text, self._data = status, text, data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)


def client(*responses):
    return Client(dict(CREDS), session=FakeSession(responses))


def test_paging_follows_next_page_to_the_end():
    c = client(
        FakeResponse(data={"numbers": [{"n": 1}], "next_page": f"{BASE_URL}/p2"}),
        FakeResponse(data={"numbers": [{"n": 2}], "next_page": None}),
    )
    assert c.numbers("1") == [{"n": 1}, {"n": 2}]
    assert [call[1] for call in c.session.calls][1] == f"{BASE_URL}/p2"


def test_api_error_text_is_scrubbed():
    c = client(FakeResponse(status=500, text=f"oops {SECRET}"))
    with pytest.raises(ApiError) as exc:
        c.accounts()
    assert SECRET not in str(exc.value) and exc.value.status == 500


def test_network_failure_on_a_read_is_an_api_error():
    with pytest.raises(ApiError):
        client(requests.ConnectionError(f"down {SECRET}")).accounts()


def test_post_sms_returns_status_and_scrubbed_text_and_sends_once():
    c = client(FakeResponse(status=406, text=f'{{"errors": "{SECRET}"}}'))
    status, text = c.post_sms("123456", "+18005550100", "+14805550123", "hi")
    assert status == 406 and SECRET not in text
    method, url, kw = c.session.calls[0]
    assert (method, url) == ("POST", f"{BASE_URL}/api/v1/accounts/123456/sms")
    assert kw["json"]["to"] == "+14805550123" and len(c.session.calls) == 1


def test_post_network_failure_means_outcome_unknown():
    with pytest.raises(SendOutcomeUnknown) as exc:
        client(requests.Timeout(f"slow {SECRET}")).post_sms("1", "+18005550100", "+14805550123", "hi")
    assert SECRET not in str(exc.value)
