from datetime import datetime, timezone

import pytest

from ctm_text import (
    already_sent, bad_chars, build_plan, confirm_code, measure, normalize_phone, read_ledger, record_send,
)

# 18:30 UTC is 11:30 in Phoenix (MST, no DST) and 14:30 in New York (EDT).
NOON_ISH = datetime(2026, 10, 15, 18, 30, tzinfo=timezone.utc)
COPY = "Hi Sam, this is Alex from the practice. We sent over the payment link you asked for. Let me know if you got it."


def plan(**over):
    args = dict(account="123456", from_raw="+18005550100", to_raw="+14805550123", body=COPY, tz_name="America/Phoenix", now=NOON_ISH)
    args.update(over)
    return build_plan(**args)


@pytest.mark.parametrize("raw,want", [
    ("+14805550123", "+14805550123"),
    ("(480) 555-0123", "+14805550123"),
    ("480-555-0123", "+14805550123"),
    ("1 480 555 0123", "+14805550123"),
    ("+44 20 7183 8750", "+442071838750"),
])
def test_normalize_phone(raw, want):
    assert normalize_phone(raw) == want


@pytest.mark.parametrize("raw", ["abc", "", "480999", "48x05550123", "+0123456789"])
def test_normalize_phone_rejects(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_sample_copy_is_one_gsm7_segment():
    assert measure(COPY) == ("GSM-7", len(COPY), 1)


def test_segment_edges():
    assert measure("a" * 160)[2] == 1
    assert measure("a" * 161)[2] == 2
    assert measure("a" * 306)[2] == 2
    assert measure("a" * 307)[2] == 3


def test_extended_characters_cost_two_units():
    assert measure("[ok]") == ("GSM-7", 6, 1)
    assert measure("{" * 81)[2] == 2  # 162 units


def test_one_bad_character_flips_the_whole_text_to_ucs2():
    assert measure("see you - 5pm") == ("GSM-7", 13, 1)
    assert measure("see you — 5pm")[0] == "UCS-2"
    assert bad_chars("it’s \U0001F600") == ["’", "\U0001F600"]


def test_clean_plan_has_no_problems_and_reads_the_recipient_clock():
    p = plan()
    assert p.problems == ()
    assert p.in_window
    assert p.local_time.endswith("11:30")


def test_window_edges():
    assert not plan(now=datetime(2026, 10, 15, 14, 59, tzinfo=timezone.utc)).in_window  # 07:59 Phoenix
    assert plan(now=datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)).in_window  # 08:00
    assert plan(now=datetime(2026, 10, 16, 3, 59, tzinfo=timezone.utc)).in_window  # 20:59
    assert not plan(now=datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)).in_window  # 21:00


def test_outside_window_is_a_problem():
    p = plan(now=datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc))
    assert any("8am to 9pm" in x for x in p.problems)


def test_problems_collected():
    assert any("not GSM-7" in x for x in plan(body="back soon — thanks").problems)
    assert any("empty" in x for x in plan(body="  ").problems)
    assert any("segment cap" in x for x in plan(body="a" * 500).problems)
    assert any("same number" in x for x in plan(to_raw="+18005550100").problems)
    assert any("toll-free" in x for x in plan(to_raw="+442071838750", tz_name="Europe/London").problems)


def test_toll_free_to_canada_is_allowed():
    assert plan(to_raw="+14165550123", tz_name="America/Toronto").problems == ()


def test_unknown_timezone_is_a_clean_error():
    with pytest.raises(ValueError, match="timezone"):
        plan(tz_name="Mars/Olympus")


def test_confirm_code_is_stable_and_binds_every_field():
    base = confirm_code("123456", "+18005550100", "+14805550123", COPY)
    assert base == confirm_code("123456", "+18005550100", "+14805550123", COPY)
    assert len(base) == 8
    assert base != confirm_code("123457", "+18005550100", "+14805550123", COPY)
    assert base != confirm_code("123456", "+18005550199", "+14805550123", COPY)
    assert base != confirm_code("123456", "+18005550100", "+14805550124", COPY)
    assert base != confirm_code("123456", "+18005550100", "+14805550123", COPY + "!")
    assert plan().code == base
    assert plan(now=datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)).code == base  # clock never moves the code


def test_ledger_keeps_codes_and_last_four_only(tmp_path):
    path = tmp_path / "out" / "sends.jsonl"
    assert read_ledger(path) == []
    p = plan()
    record_send(path, p, "sent", 200, now=NOON_ISH)
    raw = path.read_text()
    assert "0123" in raw and p.code in raw
    assert "4805550123" not in raw and "Sam" not in raw
    row = already_sent(read_ledger(path), p.code)
    assert row and row["outcome"] == "sent"


def test_rejected_sends_do_not_block_a_second_try(tmp_path):
    path = tmp_path / "sends.jsonl"
    p = plan()
    record_send(path, p, "rejected", 406)
    assert already_sent(read_ledger(path), p.code) is None
    record_send(path, p, "unknown", 0)
    assert already_sent(read_ledger(path), p.code)["outcome"] == "unknown"
