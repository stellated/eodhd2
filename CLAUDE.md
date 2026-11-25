# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Personal Python library that ingests EODHD market data (daily/intraday OHLCV) into a
local SQLite cache, with pandas/polars conversion helpers, plus a separate module that
parses "StockDataAnalytics" daily tip emails (.eml) into the same database — all to
support backtesting tips against real price data.

`/src` — the library code · `/scripts` — Ian's scratch/playpen scripts (not production) ·
`/tests` — pytest suite · `/doc` — design and context docs.

## Context docs (read these — they were written for a fresh LLM session)

@doc/HUMAN_CONTEXT.md
@doc/TECHNICAL_SPEC.md
@doc/DESIGN_DECISIONS.md
@doc/LIMITATIONS.md
@doc/CODE_REVIEW_2026-09-11.md

The code review is a dated snapshot of known bugs/drift as of 2026-09-11 — check it
against the current code rather than assuming it's still all unresolved, and update or
remove entries there as they're fixed.

## Environment

- Python venv at `.venv/`; dependencies in `requirements.txt` (pip, no version pins).
- Runtime config comes from a `.env` file (not committed) with: `EODHD_API_TOKEN`,
  `imap_server`, `imap_username`, `imap_password`, `DATA_DIR`, `system`.

## Running tests

```
pytest tests/ -q
```

All 18 tests pass as of 2026-09-12 (`ruff check tests/` is also clean) — see
`doc/CODE_REVIEW_2026-09-11.md` §2 for what was wrong and how it was fixed. `tests/`
imports `src/` via `sys.path` manipulation in `tests/conftest.py` (needed because
`src/tips_io.py` does a bare `from eodhd_io import ...`), so keep that in mind if test
collection ever fails with `ModuleNotFoundError: No module named 'eodhd_io'`.

## Linting

`ruff check .` (config in `pyproject.toml`). `tests/` is clean; the rest of the repo is
not yet — running it over `src/` surfaces pre-existing issues. Don't try to silently fix
all of these as a side effect of unrelated work; treat new warnings in files you touch as
signal, pre-existing ones as a separate cleanup task.

## Git workflow

Commit directly to `main` — no branch/PR process for this repo.

## Data requests from the analysis repos (Ian, 2026-11-25)

The analysis repos (`~/repos/analysis/<repo>`) ask for data in
`proposals/eodhd2-<topic>.md`; the template and conventions are in
`~/repos/analysis/analysis-common/project/eodhd2-requests.md`.

**Standing authority.** For routine data-request work, act without asking
and report once at the end:
- commit and push here, and commit replies into the analysis repos (pull
  first, push straight after, never force);
- build, check, upload and verify extracts and their docs;
- delete local scratch copies, and on OneDrive a copy that an upload of
  mine has just replaced (an older extract, doc or snapshot of my own);
- ticker-alias research, applied to `sda.db` through `pull_db`/`safe_push`
  outside the timers' windows, verified with `rclone cat | cmp`.

**Still ask Ian first:**
- anything that changes method or science: a new or changed rule, a
  redraw, what is in or out of a hold-out, `outcomes` logic;
- touching the hold-out (scanning it, building its extract) before Ian
  says the method is frozen;
- production writes outside the usual pull/push path, timer or systemd
  changes, schema changes to `sda.db`;
- deleting anything that isn't a superseded copy of my own;
- anything where I'm unsure which of these it is.

**Replies go by git, not through Ian.** Answer a request in
`proposals/eodhd2-<topic>.reply.md` in the requesting repo, commit and
push, then tell Ian in one line which repos have a new reply. Only
questions that are genuinely Ian's go to him.

**Before delivering any extract** (all of these, not just the builder's
own checks):
1. The builder's hard checks pass, and the request's acceptance checks
   are run and their results go in the reply.
2. Hold-out scan: every TEXT column of every table, and the extract's
   doc, searched for hold-out codes and issuer names (for date hold-outs:
   no row after the cut-off in any table).
3. No source URL or note names a party the request says to keep out (e.g.
   a cash buyer); sources are the target's own filings.
4. Every URL in the delivery was fetched or found by search this
   session, never written from memory.
5. Counts in the doc match the file (recount, don't copy).
6. `extract_info` records the commit and source snapshot; the commit is
   pushed before the build.
7. The upload is verified (`rclone cat | cmp`), and no hold-out code goes
   into a commit here (invented examples in code and tests).
