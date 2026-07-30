# Human Context — Ian Atkinson

This document is a prompt for a new LLM instance about the human partner
in this project. It covers working style, strengths, known gaps, and guidance
on how to collaborate effectively.

---

## Who Ian is

- Developer, based in Melbourne, Australia (UTC+10 / UTC+11 in daylight saving)
- Personal project: backtesting stock tips from a tipping newsletter using
  EODHD market data
- Has a subscription to StockDataAnalytics (NASDAQ tips, daily email)
- Has a subscription to EODHD that includes intraday data
- Python is his working language; uses pandas, polars, polars for display
- Uses PyCharm or similar IDE; runs code locally in a venv
- Uses git for version control; understands git diff for reviewing changes
- Uses DB Browser for SQLite to visually inspect the database

---

## Strengths

**Deliberate design thinking.** Ian asks design questions before asking for
code, wants pros and cons laid out, and will push back if a design feels wrong.
He identified the right split between eodhd_io.py and tips_io.py himself after
a brief discussion. He thinks in terms of interfaces and invariants.

**Good instincts about code structure.** He recognised when the Database class
was getting complicated and asked the right questions. He spotted that
`gmtoffset=0` for all timezones was suspicious and was right to be suspicious.

**Scope discipline.** He explicitly deferred v2.0 features during v1.0
development ("version 1.0 is getting big enough"). He doesn't gold-plate.

**Reads output carefully.** He noticed that the output for July 3rd was missing
and asked the right question. He caught the Typora date reformatting issue
(though it took a moment). He checks DBBrowser output and cross-references it.

**Comfortable with iteration.** He doesn't expect perfect code on the first
pass and is patient with debugging cycles.

---

## Weaknesses / watch points

**Copy-paste errors from formatted sources.** The D/M/YYYY vs YYYY-MM-DD
bug originated because Typora reformatted the dates in the example he shared,
and he didn't notice. He is aware of this and will warn when he thinks a
source might have been reformatted, but it's a recurring risk. Always ask
about the provenance of example data.

**Red-green colour vision deficiency.** He has stated he is red-green
colour-blind. This is relevant when discussing colour schemes in the email
HTML, chart output, or any visual. Do not assume he can distinguish red from
green. He correctly labelled "orange" as a distinct middle value but needed
confirmation of the hex values. When discussing colour, always provide hex
codes or descriptive names, not just colour names.

**Does not always notice what he's looking at.** The tip email output showed
STRO data but he was reading FLY's data when describing the context score
(said "8.0" when STRO's context was 16.0, FLY's was 8.0). He is reading
quickly and sometimes mixes up adjacent rows. When he reports "wrong" values,
check whether the data itself is correct and he's misread it.

**v1.0 vs v2.0 boundary.** He is clear about deferring lazy fetch to v2.0,
but during discussion of features he sometimes conceptually merges them. If
he starts designing a v2.0 feature mid-v1.0, gently redirect.

**Manual maintenance between sessions.** He manually applies changes from
LLM's output to his repository. He understands the workflow now (download
file, overwrite, git diff) but early in the project he was doing manual
line-by-line reconciliation, which introduced errors. If he reports a bug that
looks like a partially-applied change, check whether the correct version of
the file is in use.

---

## Opportunities for LLM to add value

**Catching format changes early.** The tip email format changed between April
and June 2026 without notice. LLM can proactively suggest that new emails
should be tested against the parser and that the test suite should be run
when a new email format is suspected.

**Flagging data quality issues.** When Ian provides example data, check for
signs of reformatting (date formats, number formats, column headers) before
accepting it at face value.

**Keeping the test suite current.** The test suite was produced late in the
project. New features should come with test additions. LLM should offer
to add tests whenever new code is produced.

**Design documentation.** Ian did not initially think to ask for design
documentation — it was offered. He found it valuable. Future significant
design decisions should be offered as additions to DESIGN_DECISIONS.md.

**Timezone and calendar awareness.** Several bugs have been timezone-related
(GMT offset always 0, exchange_calendars half-days, end defaulting to today).
When any date/time logic is being discussed or coded, LLM should proactively
think through timezone edge cases.

---

## Threats / risks

**Email format fragility.** The tips_io.py parser is tightly coupled to the
HTML structure of one newsletter's emails. The newsletter provider can change
their template at any time without notice. The test suite's specific value
assertions will catch this, but only if tests are run against new emails.
Recommend Ian runs parse_tip_email() on each new email format and checks
the output before relying on it.

**exchange_calendars maintenance.** The library's coverage dates and holiday
handling may change across versions. If exchange_calendars is upgraded and
behaviour changes (e.g. XNYS first_session moves, or half-days start being
modelled), the clip-and-warn logic and half-day workarounds may need updating.

**EODHD API changes.** If EODHD changes their CSV column names, date formats,
or endpoint structure, the parsers will break silently or with confusing errors.
The Typora reformatting incident showed that format assumptions can be wrong
in ways that are hard to debug. Adding a schema validation step to the CSV
parsers would reduce this risk.

**Growing codebase complexity.** eodhd_io.py is already ~1100 lines.
If v2.0 lazy fetch, URL construction, and more exchange support are added to
the same file, it will become hard to navigate. The split to tips_io.py was
the right first step; a further split (e.g. eodhd_fetch.py for network
operations, eodhd_schema.py for DDL and type contracts) may be warranted.

**Session continuity.** LLM has no memory between conversations. Each new
conversation starts from scratch unless Ian provides the source files and
these documents as context. Ian understands this but it creates overhead.
The practical mitigation is these three documents plus the source files.

---

## Working style preferences (stated)

- Discuss design thoroughly before generating code
- Lay out pros/cons before committing to architectural decisions
- Do not generate code when asked not to (he will explicitly say "do not
  generate code yet")
- Show what changed with a comment (e.g. `# CHANGED:`) rather than a diff
  — he uses `git diff` at his end to verify
- When delivering files, deliver the complete file (not patches) — he
  overwrites his local copy
- He will ask for diffs explicitly if he wants them
- Keep test functions focused and well-named — he reads test output
- Naming: use the established conventions (pdf for pandas, df for polars,
  op/hi/lo/cl/ac/vo for OHLCV columns)

---

## Conventions established during this project

- `pdf` = pandas DataFrame (NOT "portable document format")
- `df`  = polars DataFrame
- `db`  = Database instance or sqlite3.Connection
- `tablename` = string table name passed by caller
- OHLCV columns: op, hi, lo, cl, ac, vo
- Date columns: `date` (daily), `local_date` + `local_time` (intraday)
- Exchange suffixes without dot: "US", "LSE", "AU"
- Colour integers: 1=green, 2=yellow, 3=orange, 4=red
- expected_risk is always stored negative (it is a loss)
- Padded rows have vo=0
- All timestamps are UTC Unix epoch (int64)
- All datetimes are UTC tz-naive datetime64[us]

---

## Questions Ian has not yet been asked / may not have considered

1. What should happen when two tips() calls overlap on the same (code, date)?
   Currently INSERT OR REPLACE handles it silently. Is this always correct,
   or should there be a check?

2. What is the intended query pattern for backtesting? Knowing this would
   inform whether the current schema (one code per row, one bar per row) is
   the right shape, or whether a pivot is needed.

3. Should tips be linked to their OHLCV data by foreign key, or is the join
   always done in application code? Currently there is no foreign key
   relationship between tip_details and the price tables.

4. What happens to tips that were never fully resolved (tip_date is in the
   past but the full n2 days of data were never fetched)? Is there a cleanup
   or audit step?

5. Is there a need to track which tips were actually entered (traded), as
   distinct from tips that were received? The current schema has no
   "entered" or "outcome" field.
