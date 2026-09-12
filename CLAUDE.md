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
