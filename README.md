# ctm-connector

ctm-connector is a small command-line tool for CallTrackingMetrics (CTM), which is a service for tracking calls and sending texts. It looks up accounts, numbers and text history, and it sends a single text. I built it so an agent has one dependable way to call the CTM API, in place of hand-built requests that kept failing.

It does not schedule bulk campaigns. I haven't found a bulk route in the API, so those stay a job for the browser.

## Setup

```bash
pip install -r requirements.txt     # requests
```

Put your keys in `~/.config/ctm-bot/credentials.env` as `CTM_ACCESS_KEY` and `CTM_SECRET_KEY`, or as a single `CTM_BASIC_AUTH_TOKEN`. If the file lives somewhere else, point `CTM_CREDENTIALS_FILE` at it. The tool reads the keys itself and never prints them.

## Looking things up

Reads are free and change nothing.

```bash
python3 ctm.py accounts                                   # the accounts your keys can see
python3 ctm.py numbers --account 123456 --sms-only        # numbers that can text
python3 ctm.py texts   --account 123456 --to 4805550123   # recent text history for one number
python3 ctm.py api /api/v1/accounts.json                  # any other read-only GET
```

## Sending one text

Sending takes two steps. The first is a dry run, in which the tool prints the account, the sending number, the recipient's local time, the message and a confirm code. You read that, and then you run the same command again with `--send` and the code.

```bash
python3 ctm.py text --account 123456 --from "(800) 555-0100" --to "480-555-0123" \
    --tz America/Phoenix --body "Hello from the practice."

python3 ctm.py text --account 123456 --from "(800) 555-0100" --to "480-555-0123" \
    --tz America/Phoenix --body "Hello from the practice." --send --confirm <code>
```

The code is a hash of the account, both numbers and the exact text. If you change one word after the dry run, the code no longer matches and the send is refused.

## What it refuses

It refuses a sending number that isn't in the account or has texting switched off. It refuses anything outside 8am to 9pm in the recipient's timezone, and there is no flag to override that. It refuses text that isn't GSM-7, which is the basic character set for a text, because a single curly quote or emoji flips the whole message to a costlier encoding. It also refuses more than 3 segments, and a US toll-free sender texting outside the US and Canada.

It never retries. If a request times out, the text may or may not have gone, so the tool records the outcome as unknown and blocks the same text until you have checked the history. Sending the same text twice needs `--repeat`.

The exit code tells a caller what happened.

| Code | Meaning |
|---|---|
| 0 | It worked. |
| 2 | It refused, and nothing happened. |
| 3 | CTM rejected the request, and nothing was sent. |
| 4 | The keys are missing or were rejected. |
| 5 | The outcome is unknown, so check `texts` before doing anything else. |

## What I haven't confirmed

CTM's API reference needs JavaScript to display, so I couldn't read it, and I don't know what the message field is called. I probed with an invalid recipient so that nothing could send. That showed the route reads `to` and `from` but not a field named `body`. The tool sends the text as `msg`, `message`, `text` and `content` in the same request, and I expect the server to read whichever one is real, but I haven't confirmed that. After the first real send I'll cut the list down to the name that worked.

## Tests

`python3 -m pytest -q` runs 58 tests offline, and all of them passed on 2026-10-16.
