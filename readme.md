## Backtesting Purpose
The primary purpose of this database is to store `n1` days of data before and `n2` days after each tip’s `tip_date` to support backtesting. This enables:
- Filtering tips based on technical features of data prior to `tip_date`.
- Evaluating trades taken upon tips (or filtered subsets of tips).

/src -> the code
/doc -> project documentation
/scripts -> ian's scripting playpen
/prompts -> ian's record of dialogue with LLM