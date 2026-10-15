# ctm-connector

Command-line connector between an agent and the CallTrackingMetrics (CTM) REST API. This file holds only what the code cannot tell you. Keep it under 60 lines.

## Stack

- Python 3.11+ and `requests`. Three flat modules, which are `ctm.py` (CLI), `ctm_client.py` (HTTP and the guard) and `ctm_text.py` (checks and the ledger).
- Target is `https://api.calltrackingmetrics.com` with HTTP basic auth, using an access key and a secret key.
- Scope is lookups plus one text at a time. Scheduled bulk campaigns are out of scope because no bulk route was found (inferred, 2026-10-16).

## Commands

- Run the gates with `python3 -m pytest -q` and `ruff check .`. Tests are offline. 58/58 pass (2026-10-16).
- Run `python3 ctm.py --help`. Commands are `accounts`, `numbers`, `texts`, `api` and `text`.
- Send in two steps. `text ...` is a dry run that prints a confirm code. `text ... --send --confirm <code>` sends, and only after the owner approves that exact dry run.

## Rules

- Read keys from `~/.config/ctm-bot/credentials.env`, or from the file named in `CTM_CREDENTIALS_FILE`.
- Never print, log or commit a key. Pass every string that leaves the client through `scrub()`.
- Keep `GuardedSession.send` as the only path to the network. It allows one origin, any read, and one POST route (`/api/v1/accounts/<id>/sms`) that only `--send` arms. PUT, PATCH and DELETE are refused.
- Name the account (`account: <id> | <name>`) before every action. Refuse an account the keys cannot see. Ship no default account.
- Require `--tz`. Outside 8am to 9pm in that zone → refuse. Ship no override flag.
- Allow GSM-7 text only, 3 segments at most. One other character flips the whole text to UCS-2.
- Bind the confirm code to account, from, to and body. Leave the clock out so the code survives the wait.
- Send one POST per send. A 4xx → nothing was sent (inferred) → a retry is allowed. A timeout or 5xx → outcome `unknown` → the ledger blocks the same text until someone reads `texts`.
- Store only the code and the last four digits in `out/sends.jsonl`. Never store full numbers or message text.
- Use `--repeat` only on the owner's explicit yes.

## Open item

- POST `.../sms` reads `to` and `from` (measured, 2026-10-16). A flat or nested `body` is not read (measured, 2026-10-16).
- The real name of the message field is unknown (guess). `TEXT_KEYS` in `ctm_client.py` sends the text as `msg`, `message`, `text` and `content` in one request.
- After the first confirmed send, cut `TEXT_KEYS` to the name that worked and update the README.

## Boundaries

- Always: run a dry run before any send, show it to the owner, and add an offline test for every change.
- Ask first: any POST to CTM, a new dependency, a new write route in the guard, bulk sends.
- Never: send without the owner's explicit yes, retry a send, print a secret, push to GitHub unasked.
