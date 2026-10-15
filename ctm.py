#!/usr/bin/env python3
"""ctm: the connector CTM Bot calls instead of improvising HTTP.

Reads are free. A text goes out only through `text --send --confirm <code>`, where the code comes from the
dry run of that exact text. Run `python3 ctm.py --help`.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path

from ctm_client import ApiError, AuthMissing, Client, CtmError, Refused, SendOutcomeUnknown, scrub
from ctm_text import already_sent, build_plan, normalize_phone, read_ledger, record_send

LEDGER = Path(__file__).resolve().parent / "out" / "sends.jsonl"
EXIT_OK, EXIT_REFUSED, EXIT_API, EXIT_AUTH, EXIT_UNKNOWN = 0, 2, 3, 4, 5


def say(line: str = "") -> None:
    print(line)


def confirm_account(client: Client, account: str) -> dict:
    """The terminal version of the account chip: name the account on every run, refuse one that isn't there."""
    accounts = client.accounts()
    for row in accounts:
        if str(row.get("id")) == str(account):
            say(f"account: {row['id']} | {row.get('name')}")
            return row
    known = ", ".join(f"{a.get('id')} ({a.get('name')})" for a in accounts) or "none"
    raise Refused(f"account {account} is not visible to these keys. visible: {known}")


def cmd_accounts(args, client, **_):
    for row in client.accounts():
        say(f"{row.get('id')} | {row.get('name')}")
    return EXIT_OK


def cmd_numbers(args, client, **_):
    confirm_account(client, args.account)
    for row in client.numbers(args.account):
        if args.sms_only and not row.get("sms_enabled"):
            continue
        say(f"{row.get('number')} | {row.get('name')} | sms {'on' if row.get('sms_enabled') else 'OFF'}")
    return EXIT_OK


def cmd_texts(args, client, **_):
    confirm_account(client, args.account)
    wanted = normalize_phone(args.to) if args.to else None
    shown = 0
    for row in client.texts(args.account):
        if wanted and not _same_number(row.get("to_number"), wanted) and not _same_number(row.get("from_number"), wanted):
            continue
        body = (row.get("body") or "").replace("\n", " ")
        say(f"{row.get('sent_at')} | {row.get('direction')} | {row.get('status')} | "
            f"{row.get('from_number')} -> {row.get('to_number')} | {body[:70]}")
        shown += 1
        if shown >= args.limit:
            break
    if not shown:
        say("no matching texts on the newest page")
    return EXIT_OK


def cmd_api(args, client, **_):
    if not args.path.startswith("/api/"):
        raise Refused("api paths start with /api/")
    text = scrub(json.dumps(client.get(args.path), indent=2), client.creds)
    say(text[: args.max_chars] + ("\n... truncated" if len(text) > args.max_chars else ""))
    return EXIT_OK


def _same_number(raw, want: str) -> bool:
    try:
        return normalize_phone(str(raw)) == want
    except ValueError:
        return False


def cmd_text(args, client, now, ledger, sleep, **_):
    frm, to = normalize_phone(args.from_number), normalize_phone(args.to)
    confirm_account(client, args.account)

    mine = [n for n in client.numbers(args.account) if n.get("number") == frm]
    if not mine:
        raise Refused(f"{frm} is not a number in account {args.account}: run `ctm.py numbers --account {args.account}`")
    if not mine[0].get("sms_enabled"):
        raise Refused(f"{frm} ({mine[0].get('name')}) has texting switched off in CTM")
    say(f"from: {frm} | {mine[0].get('name')}")

    plan = build_plan(args.account, frm, to, args.body, args.tz, now=now)
    say(f"to: {plan.to_number} | recipient time {plan.local_time} ({plan.tz})")
    say(f"message: {plan.body}")
    say(f"size: {plan.encoding}, {plan.units} units, {plan.segments} segment{'s' if plan.segments != 1 else ''}")
    if plan.problems:
        for problem in plan.problems:
            say(f"REFUSED: {problem}")
        return EXIT_REFUSED

    if not args.send:
        say("dry run: nothing sent")
        say(f"confirm code: {plan.code}")
        say("to send, after the owner says yes to exactly the above:")
        say(f"  python3 ctm.py text --account {args.account} --from {plan.from_number} --to {plan.to_number} "
            f"--tz {plan.tz} --body {shlex.quote(plan.body)} --send --confirm {plan.code}")
        return EXIT_OK

    if args.confirm != plan.code:
        raise Refused(f"--confirm must be {plan.code} for exactly this text; run the dry run first and show it to the owner")
    earlier = already_sent(read_ledger(ledger), plan.code)
    if earlier and not args.repeat:
        raise Refused(
            f"this exact text already went at {earlier['ts']} (outcome: {earlier['outcome']}). "
            "check `ctm.py texts` before anything else; --repeat only on the owner's explicit yes"
        )

    try:
        status, text = client.post_sms(args.account, plan.from_number, plan.to_number, plan.body)
    except SendOutcomeUnknown as exc:
        record_send(ledger, plan, "unknown", 0, now=now)
        say(f"UNKNOWN: no answer came back ({exc}). the text may have gone. do NOT resend: check `ctm.py texts --account {args.account} --to {plan.to_number}`")
        return EXIT_UNKNOWN

    if 200 <= status < 300:
        record_send(ledger, plan, "sent", status, now=now)
        say(f"sent: HTTP {status} {text[:200]}")
        return _verify(client, plan, sleep)
    if status >= 500:
        record_send(ledger, plan, "unknown", status, now=now)
        say(f"UNKNOWN: HTTP {status} {text[:200]}. do NOT resend: check `ctm.py texts --account {args.account} --to {plan.to_number}`")
        return EXIT_UNKNOWN
    record_send(ledger, plan, "rejected", status, now=now)
    say(f"rejected, nothing sent: HTTP {status} {text[:300]}")
    return EXIT_API


def _verify(client: Client, plan, sleep) -> int:
    sleep(3)
    try:
        confirm_account(client, plan.account)  # the "after" half of the account check
        rows = client.texts(plan.account)
    except CtmError as exc:
        say(f"sent, but the history lookup failed ({exc}); check `ctm.py texts` in a minute")
        return EXIT_OK
    for row in rows:
        if _same_number(row.get("to_number"), plan.to_number) and row.get("body") == plan.body:
            say(f"history: {row.get('sent_at')} | {row.get('direction')} | {row.get('status')}")
            return EXIT_OK
    say("not in the history yet: check `ctm.py texts` in a minute")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctm.py", description=(__doc__ or "").strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("accounts", help="accounts these keys can see").set_defaults(run=cmd_accounts)

    numbers = sub.add_parser("numbers", help="tracking numbers in an account")
    numbers.add_argument("--account", required=True)
    numbers.add_argument("--sms-only", action="store_true")
    numbers.set_defaults(run=cmd_numbers)

    texts = sub.add_parser("texts", help="newest page of text history, optionally for one number")
    texts.add_argument("--account", required=True)
    texts.add_argument("--to", help="only texts to or from this number")
    texts.add_argument("--limit", type=int, default=10)
    texts.set_defaults(run=cmd_texts)

    api = sub.add_parser("api", help="read-only GET of any /api/ path, for exploring")
    api.add_argument("path")
    api.add_argument("--max-chars", type=int, default=4000)
    api.set_defaults(run=cmd_api, account=None)

    text = sub.add_parser("text", help="one text: dry run by default, --send --confirm <code> to send")
    text.add_argument("--account", required=True)
    text.add_argument("--from", dest="from_number", required=True)
    text.add_argument("--to", required=True)
    text.add_argument("--body", required=True)
    text.add_argument("--tz", required=True, help="recipient timezone, IANA name (America/Phoenix)")
    text.add_argument("--send", action="store_true")
    text.add_argument("--confirm", help="the code the dry run printed")
    text.add_argument("--repeat", action="store_true", help="allow the exact same text to go twice")
    text.set_defaults(run=cmd_text)
    return parser


def main(argv=None, *, make_client=Client, now: datetime | None = None, ledger: Path = LEDGER, sleep=time.sleep) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = make_client(allow_post=bool(getattr(args, "send", False)))
        return args.run(args, client=client, now=now, ledger=ledger, sleep=sleep)
    except AuthMissing as exc:
        say(f"auth: {exc}. ask the owner to refresh the CTM keys")
        return EXIT_AUTH
    except ApiError as exc:
        if exc.status in (401, 403):
            say(f"auth: CTM rejected the keys ({exc}). ask the owner to refresh them")
            return EXIT_AUTH
        say(f"api error: {exc}")
        return EXIT_API
    except (Refused, ValueError) as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
