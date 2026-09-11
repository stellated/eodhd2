# Code Review — eodhd2

Date: 2026-09-11
Scope: full repository (`src/`, `scripts/`, `tests/`, `doc/`), as of commit `4a672c7`.

This review reads the tracked source (`eodhd_io.py`, `tips_io.py`, `email_downloader.py`,
`scripts/utils.py`), the test suite (actually run with `pytest`), and the project's own
`doc/*.md` files, then checks the three against each other. Several findings are places
where the code has silently drifted from what the docs/tests say it does — these are
flagged as the highest priority since they'd otherwise go unnoticed.

---

## 1. Critical — data correctness

### 1.1 `tips_io.py`: colour integers no longer match the documented 1–4 scale — FIXED 2026-09-11
`_COLOUR_INT` (src/tips_io.py:45-53) used to map:

```python
"#22c55e": 1,  # green
"#eab308": 2,  # yellow
"#ca8a04": 3,  # (comment says "2")
"#f97316": 4,  # (comment says "3", i.e. orange should be 3)
"#ef4444": 5,  # (comment says "4", i.e. red should be 4)
"#854d0e": 6,
"#94a3b8": 7,
```

Every doc in the repo (module docstring, `doc/TECHNICAL_SPEC.md`, `doc/DESIGN_DECISIONS.md`)
and `tests/test_tips_io.py::test_hex_to_int` describe/assert a **1=green, 2=yellow,
3=orange, 4=red** traffic-light scale. The live mapping is a flat 1–7 sequence instead —
orange now returns `4` and red returns `5`. `src/tips_io.prior.py` (the previous, untracked
version still sitting in the tree) has the *correct* 1–4 mapping, so this looks like an
accidental regression introduced when the file was last edited, not a deliberate change.

**Impact:** every colour-coded score (pattern quality, setup, risk/reward, context, week/
month/regime colour) stored from here on is off by one or more for orange/red/amber values.
`doc/HUMAN_CONTEXT.md` notes Ian is red-green colour-blind and relies on these integers
(not the raw hex) to reason about tip quality — this is exactly the kind of silent
corruption he can't visually sanity-check.

**Fix:** restore the 1–4 mapping (as in `tips_io.prior.py`), or update every doc + the test
if a wider scale is intentional.

**Resolution:** Fixed. Before applying the fix, every score-bar fill colour was enumerated
across all captured emails in `scripts/data/html/` (~7,000+ bars): the four per-tip
attributes (pattern_quality, setup, risk_reward, context) only ever use `#22c55e` (green),
`#eab308` (yellow), and `#f97316` (orange) — `#ef4444` (red) is never observed on these four
attributes in any captured email, though it is used elsewhere (week/month % change, which
shares the same 1–4 scale) so it's kept as the top of the scale. `#ca8a04` was confirmed to
be a darker-amber alias used only for the regime-score text (matches
`doc/TECHNICAL_SPEC.md`'s "2 = yellow (#eab308, #ca8a04)") and is kept mapped to `2`.
`#854d0e` and `#94a3b8` were confirmed to be unreachable — never actually passed to
`_hex_to_int` by any parsing code path in this module — and were dropped rather than carried
along. `_COLOUR_INT` is now:

```python
"#22c55e": 1,  # green
"#eab308": 2,  # yellow
"#ca8a04": 2,  # dark amber alias for yellow (regime score text)
"#f97316": 3,  # orange
"#ef4444": 4,  # red
```

Verified by re-running `parse_tip_email()` against the 2026-04-08 NASDAQ email: STRO's
risk_reward_colour now reads `2` (was `4`), FLY's context_colour now reads `3` (was `5`), no
nulls introduced, and week/month/regime colour extraction is unaffected.

### 1.2 Compact tip cards store `'0'` instead of `None` for numeric scores — RESOLVED 2026-09-12
`src/tips_io.py:361-364` (inside `_parse_tip_card`, compact-card branch) used to have:

```python
result['pattern_quality_number'] = '0'
result['setup_number'] = '0'
result['risk_reward_number'] = '0'
result['context_number'] = '0'
```

These four fields go through `pd.to_numeric(..., errors="coerce")` in `parse_tip_email`
(not the `Int64`/nullable-int path), so `'0'` becomes a real float `0.0`, not `NaN`.

This directly contradicts:
- the module docstring: *"Numeric scores ... are None for compact cards (tips 4-20)"*
- `doc/TECHNICAL_SPEC.md`: *"pattern/setup/rr/context NUMBER fields are None"*
- `tests/test_tips_io.py::test_parse_tip_email_june_2026`: asserts `.isna().all()` for
  these columns on compact cards.

**Impact:** any downstream filtering ("only trade tips with pattern_quality_number > X")
will treat every compact-card tip (17 of the 20 tips per email, per the June+ format) as
having a real, terrible score of `0` rather than "unknown." For a backtesting pipeline this
is a real risk of silently biasing results rather than raising the missing-data flag the
codebase otherwise takes care to document.

**Resolution:** Superseded by a broader redesign rather than a narrow fix. Forensic analysis
of the score bar's rendering (across all 166 captured emails) showed the compact-card bar
height is not just a colour indicator — it's a deterministic, invertible encoding of the
same score the full cards print as a number (fixed per-quality maximums summing to the
newsletter's own "Total Score: X / 98"; a fixed 24px box; `score = height * max / 24`).
Rather than just fixing the `'0'` bug in place, the `..._number`/`..._height` column pairs
were unified into a single `..._score` column per quality — exact for tip_n 1-3, estimated
(±0.75-1.7 points depending on category) for tip_n 4-20 — with the parser now bounds- and
colour-cross-checking every score it produces and logging (not raising) on any anomaly. See
`doc/DESIGN_DECISIONS.md` ("Unified per-quality score column") for the full writeup, and
`doc/LIMITATIONS.md` for the precision caveat. Verified by re-running the new parser across
all 166 archived emails: zero warnings logged, and the worked STRO/FLY/FROG/PSNY/BETR
example from that analysis reproduces exactly.

**Fix:** set these four fields to `None` (matching the `full`-card branch behaviour and the
struct already documented everywhere else).

---

## 2. Test suite is not currently runnable

Running `pytest tests/ -q` today gives **19 failed, 10 passed, 1 error**. None of this is
pre-existing/flaky — every failure has a concrete cause:

- **`tests/test_tips_io.py` has no import statements at all.** It uses `parse_tip_email`,
  `Path`, `BeautifulSoup`, `_get_html`, `_parse_tip_card`, `_hex_to_int`, `logging`,
  `tips_exchange2sqlite`, `tips_sqlite2pandas` without importing any of them → every test in
  the file fails with `NameError`.
- **The `.eml` fixtures it points at don't exist.** It reads
  `tests/data/eml/2026-04-08_Daily_Stock_Pick.eml` / `2026-06-10_Daily_Stock_Pick.eml`, but
  `tests/data/` contains nothing but a stray `.DS_Store`. The real captured emails live in
  `scripts/data/eml/` with a different naming convention
  (`2026-04-08_16-36-02_Daily Stock Pick.eml` — timestamp + a space, not the underscored
  name the test expects). Fixing the imports alone would still leave these tests failing
  with `FileNotFoundError`.
- **`test_eodhd_io.py::test_fetch_daily` (and `_empty`) patch mocks in the wrong parameter
  order.** `@mock.patch` decorators apply bottom-up, so for
  ```python
  @mock.patch("src.eodhd_io._fetch_with_retry")
  @mock.patch("src.eodhd_io._get_calendar")
  def test_fetch_daily(mock_fetch, mock_get_calendar):
  ```
  `mock_fetch` actually receives the `_get_calendar` mock and vice versa. The test then
  configures the *calendar* mock with a fake HTTP response and leaves the real
  `_fetch_with_retry` mock unconfigured, so `resp.text` is a bare `MagicMock` and
  `pd.read_csv(io.StringIO(resp.text))` raises `TypeError: initial_value must be str or
  None, not MagicMock`.
- **A misplaced `@given` decorator silently disables the intended hypothesis test.** In
  `test_eodhd_io.py:216-238`:
  ```python
  @given(df=data_frames(...))
  def test_min_date(df):
      print(...)
  @settings(max_examples=50)
  def test_pandas_polars_roundtrip_hypothesis(df):
      ...
  ```
  The `@given(...)` strategy meant for the round-trip test is attached to `test_min_date`
  instead (which does nothing but a `print`). `test_pandas_polars_roundtrip_hypothesis` is
  left with only `@settings` and no `@given`, so pytest treats `df` as a missing fixture
  and errors at collection — **the actual pandas↔polars round-trip property test never
  runs.** Separately, the `data_frames(columns=[...], rows=...)` call itself is invalid
  under the installed `hypothesis` version (`InvalidArgument: Must specify a dtype for all
  columns when combining rows with columns`), so even a correctly-attached `@given` would
  still fail today.
- `tests/test_eodhd_io_prior.py` is an older, superseded copy of `test_eodhd_io.py`
  (untracked, `git ls-files` doesn't list it) that duplicates the same test names and fails
  for its own reasons (missing CSV fixtures under `tests/data/csv/`, etc.). Having both the
  "prior" and current test files in the same `tests/` directory means `pytest` collects and
  runs both, doubling noise for no benefit.

**Net effect:** the test suite currently gives no real signal. It would not have caught
either of the correctness bugs in §1. Recommend triaging in this order: (a) delete or
archive `test_eodhd_io_prior.py`, (b) add the missing imports to `test_tips_io.py` and
either commit real fixture `.eml` files under `tests/data/eml/` with the exact names the
tests expect, or fix the paths to point at `scripts/data/eml/`, (c) fix the two decorator
bugs in `test_eodhd_io.py`, (d) confirm the `hypothesis.extra.pandas.data_frames` call
against the currently pinned `hypothesis` version.

---

## 3. Documentation drift

- `doc/TECHNICAL_SPEC.md` documents `Database.to_polars(tablename, **kwargs)` and
  `Database.to_csv(tablename, csv_path, **kwargs)` as existing methods. Neither exists in
  `src/eodhd_io.py` — only `to_pandas`, `from_csv`, `from_pandas`, `from_polars`, and
  `fetch` are implemented. Either the methods were never written or were removed; the spec
  should be corrected or the methods added.
- `doc/TECHNICAL_SPEC.md` contradicts itself on the `expected_risk` sign convention within
  the same document: the body text and "Key test assertions" section both say values are
  stored **negative** (e.g. `expected_risk=-0.52`), then a trailing `### Sign Convention`
  section at the very end says it's stored as **positive**. The actual code
  (`src/tips_io.py:254,332`) stores `abs(...)` — i.e. positive — matching only the second,
  contradicting section. Delete the stale negative-convention text (and the matching stale
  assertions higher up in the doc) so there's one source of truth.
- `doc/HUMAN_CONTEXT.md` / `doc/TECHNICAL_SPEC.md` describe `_is_full_card()` as the format
  discriminator function; the actual code uses an inline `if tip_n <= 3:` check in
  `_parse_tip_card` — there is no `_is_full_card` function. Harmless but will confuse a
  future reader searching for it.

---

## 4. Dead code / cruft worth cleaning up

- `src/eodhd_io.py:485-537` — a full commented-out previous implementation of
  `pandas2sqlite` (~50 lines) sits directly above the live one. Safe to delete now that
  it's superseded (git history preserves it if ever needed).
- `_n_sessions_after()` (`src/eodhd_io.py:122`) and `_extract_bg_colour()`
  (`src/tips_io.py:69`) are defined but never called anywhere in the tracked codebase.
- `pandas2polars()` (`src/eodhd_io.py:423-455`) has debug `print()` calls (including
  dumping the full head of every DataFrame with `display.max_columns=None`) left in on
  every call — this is library code called from `Database.to_pandas()`'s hot path via
  callers, so it will spam stdout in any script or notebook that uses it. Should use
  `logging.debug(...)` (already set up elsewhere in the file) or be removed.
- `is_connection_closed()` (`src/eodhd_io.py:810-820`) carries a leftover note-to-self in
  its docstring ("I think I've dispensed with this crap") — fine to leave functionally but
  worth tidying since it's shipped as part of the module docstring surface.
- Untracked backup/scratch files living inside `src/` rather than `scripts/`:
  `src/tips_io.prior.py`, `src/20260806.eodhd_io.py`, `src/20260806.tips_io.py`,
  `src/test2.py`, `src/check_halfday.py`. None of these are tracked by git, so they're not
  part of the shipped module, but their presence in `src/` (rather than `scripts/`, which
  the readme already designates as "ian's scripting playpen") makes it easy to accidentally
  `import` the wrong version or lose track of which file is canonical — worth moving or
  deleting now that they've served their purpose (especially `tips_io.prior.py`, which as
  noted in §1.1 actually has the *correct* colour mapping).

---

## 5. Security / secrets hygiene

- **There is no `.gitignore` anywhere in the repository.** `git status` currently shows
  `.env`, `.idea/`, `.DS_Store` files, `test.db`, `.venv/`, `.hypothesis/`, `.pytest_cache/`,
  and the entire `scripts/data/` tree (hundreds of `.eml`/`.html`/`.csv` files) as untracked
  — all one `git add -A` away from being committed. `tests/data/.DS_Store` is in fact
  already staged (`git status` shows `A tests/data/.DS_Store`).
- **`.env` (repo root, untracked but unprotected) contains a live IMAP email password and a
  live EODHD API token in plaintext.** It happens not to be tracked by git today, but
  without a `.gitignore` entry that's incidental rather than enforced. Recommend adding a
  `.gitignore` with at least `.env`, `*.db`, `.DS_Store`, `__pycache__/`, `.venv/`, `.idea/`,
  `.pytest_cache/`, `.hypothesis/` — and rotating the IMAP password and EODHD token since
  they've now been read in plaintext during this review.
- `src/email_downloader.py:139` does `print(USERNAME, PASSWORD)` right before running,
  echoing the IMAP password in cleartext to stdout (and to any terminal scrollback/log
  capture). Should be removed.
- `src/email_downloader.py`'s `__main__` block is currently broken and would crash
  immediately if run: `TARGET_FOLDER = "../emails"` is assigned as a plain `str`, then
  `TARGET_FOLDER.is_dir()` is called on it (line 131) — `AttributeError`, since `str` has no
  `is_dir()`. The `sirius` branch also references `Path(...)` (line 124) but `Path` is never
  imported in this file. `scripts/ian_test.py` (which does import `Path` and reassigns
  `EMAIL_FOLDER` correctly) appears to be the actual, working entry point — `email_
  downloader.py`'s own `__main__` looks like leftover/never-fixed scaffolding rather than a
  runnable script.

---

## 6. Minor / style notes

- `readme.md` line 9 ends with a stray trailing `"` (`/tests -> automated testing (needs
  work)"`) — looks like a copy/paste artifact from formatted markdown.
- `Database.__init__` creates the parent directory with `self.db_path.parent.mkdir()`
  (`src/eodhd_io.py:856`) without `parents=True`; if the grandparent directory is also
  missing this raises `FileNotFoundError` instead of creating the full path.
- `_eodhd_fetch_csv` calls `resp.raise_for_status()` *outside* the `@retry`-wrapped
  `_fetch_with_retry`, so the tenacity retry only covers transport-level exceptions (DNS/
  connection errors), not HTTP 4xx/5xx responses from EODHD. This may be intentional (you
  don't want to retry a 401), but it's worth a one-line comment saying so, since at first
  glance it looks like the retry should cover HTTP errors too.
- `scripts/ian_test.py` and `scripts/data/test.db` are working scratch files (per the
  readme, this is expected/fine) but `scripts/data/test.db` and the large `eml/`/`html/`/
  `csv/` trees under `scripts/data/` are a lot of personal data (stock-tip emails) sitting
  untracked in a git working directory with no `.gitignore` — see §5.

---

## Summary — suggested priority order

1. Fix the colour-scale regression in `tips_io.py` (§1.1) — silent, hard-to-detect data
   corruption, worst kind of bug to have missed.
2. Fix compact-card numeric fields being `'0'` instead of `None` (§1.2) — same category.
3. Add a `.gitignore` and rotate the credentials in `.env` (§5).
4. Get the test suite green again (§2) so it can actually catch regressions like #1 and #2
   in future — right now it would not have caught either.
5. Reconcile `doc/TECHNICAL_SPEC.md` with the real `Database` API and pick one
   `expected_risk` sign convention (§3).
6. Clean up dead code and stray backup files (§4) at a convenient time — no urgency.
