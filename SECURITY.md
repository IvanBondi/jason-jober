# Security Policy — Jason Jober

## Overview

Jason Jober is a personal job-monitoring bot. This document describes the security controls in place and the rules governing access and data handling.

## Telegram Access Control

Only whitelisted Telegram user IDs may interact with the bot. Any message from an unknown user is silently dropped and logged.

| User ID    | Access |
|------------|--------|
| 288775465  | Owner  |

To add a user, edit `ALLOWED_TELEGRAM_USER_IDS` in `security.py` and redeploy.

## Prompt Injection Detection

All incoming Telegram message text is scanned for known prompt injection patterns before processing:

- `ignore previous instructions`
- `forget your rules`
- `act as`
- `new instructions`
- `system prompt`
- `ignore all prior` / `disregard your/all/previous`
- `you are now a/an`
- `pretend you are` / `pretend to be`
- `roleplay as`

Messages matching any pattern are dropped and logged. The same scan is applied to RSS and email fields before relevance filtering.

## RSS & Email Input Sanitization

Every RSS entry and Gmail-parsed job dict is passed through `sanitize_entry()` before any filtering or display:

- Whitespace is normalised (collapse runs, strip edges)
- Fields are truncated (`title`/`summary`/`description` → 2000 chars, `link` → 500 chars)
- Injection patterns found in field content are replaced with `[REDACTED]`

## Security Logging

All suspicious events are written to `security.log` in the project directory with timestamps:

- Unauthorised Telegram user attempts
- Injection patterns detected in messages or feed fields
- Field modifications during sanitization

## Credentials Audit

### Environment variables (correct — not hardcoded)

| Variable               | Purpose                        |
|------------------------|--------------------------------|
| `TELEGRAM_TOKEN`       | Telegram bot token             |
| `CHAT_ID`              | Destination chat ID            |
| `GOOGLE_CREDENTIALS_JSON` | Service account JSON        |
| `GMAIL_TOKEN_JSON`     | Gmail OAuth token JSON         |

### Hardcoded fallbacks (low-risk but noted)

| Location                | Value                                  | Risk   |
|-------------------------|----------------------------------------|--------|
| `main.py:29` `GOOGLE_SHEET_ID` | `1HO22ANItDntqzK4oM2bQAQHsAk0qUITxP-ND1wJqhic` | Low — public sheet ID, not a secret, but should be moved to an env var for hygiene |

### Local credential files (gitignored)

The following files are listed in `.gitignore` (`*.json`) and must never be committed:

- `lucky-wonder-494514-b0-a80366374894.json` — Google service account key
- `token_gmail.json` — Gmail OAuth refresh token
- `client_secret_*.json` — OAuth client secret

**Action required:** Ensure these files are set as Railway environment variables (`GOOGLE_CREDENTIALS_JSON`, `GMAIL_TOKEN_JSON`) so no secret material lives on disk in production.

## Recommendations

1. Move `GOOGLE_SHEET_ID` out of the source code into a Railway env var.
2. Rotate the Google service account key periodically.
3. Review `security.log` occasionally for anomalous activity.
4. Keep `ALLOWED_TELEGRAM_USER_IDS` minimal.
