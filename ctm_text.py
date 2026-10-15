"""Single-text planning: phone and GSM-7 checks, the send window, the confirm code and the send ledger."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENDED = frozenset("^{}\\[~]|€\f")  # each costs two units
E164 = re.compile(r"\+[1-9]\d{7,14}")
TOLL_FREE = re.compile(r"\+1(?:800|833|844|855|866|877|888)\d{7}")
WINDOW_START, WINDOW_END = 8, 21  # recipient local time, 8am up to (not including) 9pm
MAX_SEGMENTS = 3


def normalize_phone(raw: str) -> str:
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if re.search(r"[A-Za-z]", text) or not digits:
        raise ValueError(f"{raw!r} is not a phone number: use +<country><number>, e.g. +14805550123")
    if text.startswith("+"):
        candidate = "+" + digits
    elif len(digits) == 10:
        candidate = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        raise ValueError(f"cannot read {raw!r} as a phone number: use +<country><number>, e.g. +14805550123")
    if not E164.fullmatch(candidate):
        raise ValueError(f"{raw!r} does not look like an E.164 number: use +<country><number>")
    return candidate


def bad_chars(body: str) -> list[str]:
    return sorted({c for c in body if c not in GSM7_BASIC and c not in GSM7_EXTENDED})


def measure(body: str) -> tuple[str, int, int]:
    """(encoding, units, segments). One non-GSM-7 character flips the whole text to UCS-2."""
    if not bad_chars(body):
        units = sum(2 if c in GSM7_EXTENDED else 1 for c in body)
        return "GSM-7", units, 1 if units <= 160 else -(-units // 153)
    units = len(body.encode("utf-16-le")) // 2
    return "UCS-2", units, 1 if units <= 70 else -(-units // 67)


@dataclass(frozen=True)
class TextPlan:
    account: str
    from_number: str
    to_number: str
    body: str
    tz: str
    encoding: str
    units: int
    segments: int
    local_time: str
    in_window: bool
    problems: tuple[str, ...]

    @property
    def code(self) -> str:
        return confirm_code(self.account, self.from_number, self.to_number, self.body)


def confirm_code(account: str, from_number: str, to_number: str, body: str) -> str:
    """Ties a send to the exact dry run the owner said yes to. Independent of the clock, so it survives the wait."""
    return hashlib.sha256("\x1f".join((account, from_number, to_number, body)).encode()).hexdigest()[:8]


def build_plan(account: str, from_raw: str, to_raw: str, body: str, tz_name: str, now: datetime | None = None) -> TextPlan:
    frm, to = normalize_phone(from_raw), normalize_phone(to_raw)
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {tz_name!r}: use an IANA name like America/Phoenix") from None
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    in_window = WINDOW_START <= local.hour < WINDOW_END
    encoding, units, segments = measure(body)

    problems: list[str] = []
    if not body.strip():
        problems.append("the message is empty")
    bad = bad_chars(body)
    if bad:
        shown = ", ".join(f"{c!r} U+{ord(c):04X}" for c in bad[:6])
        problems.append(f"not GSM-7 ({shown}): use straight quotes and hyphens, no emoji")
    if segments > MAX_SEGMENTS:
        problems.append(f"{segments} segments is over the {MAX_SEGMENTS} segment cap: shorten it")
    if frm == to:
        problems.append("from and to are the same number")
    if TOLL_FREE.fullmatch(frm) and not to.startswith("+1"):
        problems.append("a US toll-free number can't text outside the US and Canada")
    if not in_window:
        problems.append(f"outside 8am to 9pm recipient time ({local:%H:%M} in {tz_name}): wait for the window")

    return TextPlan(
        account=account, from_number=frm, to_number=to, body=body, tz=tz_name, encoding=encoding, units=units,
        segments=segments, local_time=f"{local:%a %H:%M}", in_window=in_window, problems=tuple(problems),
    )


# The ledger holds codes and last-four digits only: no message text, no full numbers.

def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def already_sent(ledger: list[dict], code: str) -> dict | None:
    for row in ledger:
        if row.get("code") == code and row.get("outcome") in ("sent", "unknown"):
            return row
    return None


def record_send(path: Path, plan: TextPlan, outcome: str, http_status: int, now: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "code": plan.code,
        "account": plan.account,
        "from_last4": plan.from_number[-4:],
        "to_last4": plan.to_number[-4:],
        "segments": plan.segments,
        "outcome": outcome,
        "http_status": http_status,
    }
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
