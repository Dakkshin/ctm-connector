from datetime import datetime, timezone

import ctm
from ctm_client import SendOutcomeUnknown

NOW = datetime(2026, 10, 15, 18, 30, tzinfo=timezone.utc)  # 11:30 in Phoenix
BODY = "Hi Sam, this is Alex from the practice. We sent over the payment link you asked for."
FROM, TO = "+18005550100", "+14805550123"


class FakeClient:
    def __init__(self, *, post=(202, "{}"), history=None, sms_on=True, numbers=True):
        self.creds = {}
        self.post_result = post
        self.history = history if history is not None else []
        self.sms_on = sms_on
        self.has_number = numbers
        self.posts = []

    def accounts(self):
        return [{"id": 123456, "name": "Example Practice - Main"}]

    def numbers(self, account):
        return [{"number": FROM, "name": "Main line", "sms_enabled": self.sms_on}] if self.has_number else []

    def texts(self, account):
        return self.history

    def post_sms(self, account, frm, to, body):
        self.posts.append((account, frm, to, body))
        if isinstance(self.post_result, Exception):
            raise self.post_result
        return self.post_result


def run(argv, fake, tmp_path, capsys, now=NOW):
    code = ctm.main(argv, make_client=lambda **kw: fake, now=now, ledger=tmp_path / "sends.jsonl", sleep=lambda s: None)
    return code, capsys.readouterr().out


def base(*extra):
    return ["text", "--account", "123456", "--from", FROM, "--to", "(480) 555-0123", "--tz", "America/Phoenix", "--body", BODY, *extra]


def dry_code(tmp_path, capsys):
    code, out = run(base(), FakeClient(), tmp_path, capsys=capsys)
    assert code == 0
    return out.split("confirm code: ")[1].split()[0]


def test_dry_run_sends_nothing_and_prints_the_code_and_next_command(tmp_path, capsys):
    fake = FakeClient()
    code, out = run(base(), fake, tmp_path, capsys=capsys)
    assert code == 0 and fake.posts == []
    assert "account: 123456 | Example Practice" in out
    assert "from: +18005550100 | Main line" in out
    assert "dry run: nothing sent" in out and "--send --confirm" in out
    assert "to: +14805550123" in out


def test_send_with_the_dry_run_code_posts_exactly_once_and_logs(tmp_path, capsys):
    secret_code = dry_code(tmp_path, capsys)
    history = [{"to_number": "+14805550123", "body": BODY, "status": "delivered", "sent_at": "now", "direction": "outbound"}]
    fake = FakeClient(history=history)
    code, out = run(base("--send", "--confirm", secret_code), fake, tmp_path, capsys=capsys)
    assert code == 0 and len(fake.posts) == 1
    assert fake.posts[0] == ("123456", FROM, TO, BODY)
    assert "sent: HTTP 202" in out and "delivered" in out
    assert "4805550123" not in (tmp_path / "sends.jsonl").read_text()


def test_send_without_the_right_code_is_refused(tmp_path, capsys):
    fake = FakeClient()
    for extra in (["--send"], ["--send", "--confirm", "deadbeef"]):
        code, out = run(base(*extra), fake, tmp_path, capsys=capsys)
        assert code == 2 and "REFUSED" in out
    assert fake.posts == []


def test_changing_one_word_invalidates_the_code(tmp_path, capsys):
    secret_code = dry_code(tmp_path, capsys)
    argv = base("--send", "--confirm", secret_code)
    argv[argv.index("--body") + 1] = BODY + " Thanks"
    fake = FakeClient()
    code, _ = run(argv, fake, tmp_path, capsys=capsys)
    assert code == 2 and fake.posts == []


def test_same_text_never_goes_twice_without_repeat(tmp_path, capsys):
    secret_code = dry_code(tmp_path, capsys)
    args = base("--send", "--confirm", secret_code)
    assert run(args, FakeClient(), tmp_path, capsys=capsys)[0] == 0
    fake = FakeClient()
    code, out = run(args, fake, tmp_path, capsys=capsys)
    assert code == 2 and fake.posts == [] and "already went" in out
    assert run([*args, "--repeat"], FakeClient(), tmp_path, capsys=capsys)[0] == 0


def test_outside_the_window_refuses_even_with_a_valid_code(tmp_path, capsys):
    secret_code = dry_code(tmp_path, capsys)
    late = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)  # 23:00 Phoenix
    fake = FakeClient()
    code, out = run(base("--send", "--confirm", secret_code), fake, tmp_path, now=late, capsys=capsys)
    assert code == 2 and fake.posts == [] and "8am to 9pm" in out


def test_server_rejection_is_reported_as_not_sent_and_can_be_retried(tmp_path, capsys):
    secret_code = dry_code(tmp_path, capsys)
    fake = FakeClient(post=(406, '{"errors":{"body":["can\'t be blank"]}}'))
    code, out = run(base("--send", "--confirm", secret_code), fake, tmp_path, capsys=capsys)
    assert code == 3 and "rejected, nothing sent" in out
    assert run(base("--send", "--confirm", secret_code), FakeClient(), tmp_path, capsys=capsys)[0] == 0


def test_timeout_is_unknown_and_blocks_a_blind_resend(tmp_path, capsys):
    secret_code = dry_code(tmp_path, capsys)
    code, out = run(base("--send", "--confirm", secret_code), FakeClient(post=SendOutcomeUnknown("timed out")), tmp_path, capsys=capsys)
    assert code == 5 and "do NOT resend" in out
    code, out = run(base("--send", "--confirm", secret_code), FakeClient(), tmp_path, capsys=capsys)
    assert code == 2 and "already went" in out


def test_number_not_in_account_or_texting_off_is_refused(tmp_path, capsys):
    assert run(base(), FakeClient(numbers=False), tmp_path, capsys=capsys)[0] == 2
    code, out = run(base(), FakeClient(sms_on=False), tmp_path, capsys=capsys)
    assert code == 2 and "texting switched off" in out


def test_wrong_account_is_refused_with_the_visible_list(tmp_path, capsys):
    argv = base()
    argv[argv.index("--account") + 1] = "111"
    code, out = run(argv, FakeClient(), tmp_path, capsys=capsys)
    assert code == 2 and "not visible" in out and "123456" in out


def test_bad_phone_or_timezone_is_a_clean_refusal(tmp_path, capsys):
    argv = base()
    argv[argv.index("--to") + 1] = "abc"
    assert run(argv, FakeClient(), tmp_path, capsys=capsys)[0] == 2
    argv = base()
    argv[argv.index("--tz") + 1] = "Nowhere/Land"
    assert run(argv, FakeClient(), tmp_path, capsys=capsys)[0] == 2


def test_api_command_refuses_non_api_paths(tmp_path, capsys):
    code, out = run(["api", "https://evil.example/x"], FakeClient(), tmp_path, capsys=capsys)
    assert code == 2 and "REFUSED" in out
