# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `/auth` no longer dead-ends on accounts with a cloud password (2FA). The
  `SessionPasswordNeededError` branch previously replied "Password-based login
  is not yet supported" and gave up, leaving the scraping user client
  unauthorized. Because an unauthorized user client silently scrapes nothing,
  the bot reported itself healthy while collecting no messages at all — the
  failure surfaced as "no relevant news" rather than as an error. The branch now
  hands the operator the exact host procedure that does work.
- `--auth` now fails with a clear message instead of blocking forever when
  stdin is not a TTY.
- Removed a stray `</b>` from the `/auth` phone-step error reply.

### Security

- **Decision: the cloud password is supplied out-of-band, never over the bot
  chat.** Two designs were considered for completing 2FA login:

  1. An in-chat `WAIT_PASSWORD` dialog step calling `sign_in(password=...)`,
     with the password message deleted after use.
  2. Out-of-band login on the host over a TTY (`teledigest --auth`), with
     `/auth` reporting status and pointing at that procedure.

  **(2) was chosen.** A login code is single-use and expires in minutes; a cloud
  password is long-lived, reusable, and is precisely the credential meant to
  survive a stolen session. Accepting it in chat would write it into the message
  history of the account it protects, so session theft would escalate to full
  account takeover. `delete_messages()` does not close that gap: deletion is
  best-effort and racy, and in the exact situation where `/auth` is needed the
  bot may not be running to receive and delete the message. The convenience
  argument for (1) is also weak for this project — the out-of-band mechanism
  already existed (Telethon's `client.start()` prompts for the password via
  `getpass`), and re-authorizing already requires host access to stop the
  service holding the session file open.
- `/auth` error replies now scrub any value the operator typed before echoing
  Telethon exception text back to the chat; previously `except Exception` echoed
  `{e}` verbatim, which could quote back the submitted login code.

## [0.1.0] - 2025-12-20

Initial public release of Teledigest — an LLM-driven framework for building
Telegram digest and channel-analysis bots.

### Added

- Initial implementation of the Telegram digest bot with Telethon-based user
  client and bot client
- TOML-based bot configuration (`teledigest.conf`)
- SQLite message store using `sqlite-utils`; digest is generated from messages
  received in the last 24 hours
- OpenAI-powered summarization pipeline
- Scheduler with support for hourly and minute-level (`summary_minute`) digest
  intervals
- Bot commands: `/start`, `/help`, `/digest` (alias `/today`), `/auth`
- Bot-based `/auth` flow for authenticating the user (Telethon) client without
  direct CLI access
- `--auth` CLI option for one-time interactive Telegram authentication
- Configurable Telegram session directory via `[telegram] session_dir`
- Prettier bot command output formatting
- Dockerfile and `docker-compose.yml` for containerised deployments
- Pre-commit hooks (black, isort, mypy, ruff)
- GitHub Actions CI workflow with markdown linter and isort checks
- pytest suites covering the TOML config parser and database layer

### Changed

- Migrated dependency management from `requirements.txt` to Poetry
- Reorganised source tree into dedicated modules
  (`config`, `db`, `llm`, `scheduler`, `bot`, `client`, `cli`)
- Renamed LLM config key `token` → `api_key` to match OpenAI SDK terminology

### Removed

- `/ping` bot command

### Fixed

- OpenAI client usage repaired after migration to the new SDK API (`>=2.x`)
- `bot_client` initialisation order — ensured it is ready before first use
- CLI no longer prints a full traceback on expected errors unless `--debug` is
  passed
- Python patch version included in CI venv cache key to prevent stale caches

[0.1.0]: https://github.com/igoropaniuk/teledigest/releases/tag/v0.1.0
