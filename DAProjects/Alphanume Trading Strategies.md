Alphanume Trading Strategies



All projects: IG lend at interbank + 3% for long positions, Interbank - 3% for shorts.

Relevant interbank interest rate in Australia is RBA Cash rate which is 4.6% in October 2026

Relevant interbank interest rate in USA is SOFR (Secured Overnight Financing Rate) which is 3.89% in October 2026

Strategies should be stress tested at 

* unleveraged
* leveraged at interbank = 4%
* leveraged at interbank = 8%
* leveraged at interbank = 12%

History data for SOFR is needed.  Monthly data will suffice, could work with just quarterly data if I must.



All projects: whether or not tips are tradeable (and in some cases shortable) via IG Markets requires cross-referencing tips with the information in the ig_margins table.



Can measures of liquidity, such as Roll's or Corwin-Schultz spread estimators be made from daily data?  How much prior data is needed?



Project: Quant Galore Momentum Index

Main question: are the returns as good as the article depicts, or were they cherry picking their start and end points?

Article at https://quant-galore.medium.com/3-real-quant-trading-strategies-you-can-run-today-code-included-553b20b5e896

See also: https://github.com/quantgalore/Time-Series-Momentum and https://www.quant-galore.com/p/a-junior-quants-guide-to-time-series 

Also

* attempt to replicate the index
* attempt to create a short-selling version of the index

Data required:

* daily data for the stocks over the month that they are in the index and for the prior year
* no need for 5m data
* Historical Optioned Tickers 



Project: Next day movers (ORB trade)

Main question: does ORB make good profit?  

Article at https://www.alphanume.com/guides/next-day-movers-guide

Also

* Does that Reddit post add value to ORB here?

Data required

* daily 5m data for each tip covering the day of the tip and the 20 prior days



Project: Reverse Splits

Main question: are these sufficiently liquid and short-sellable via IG Markets?

Article at https://quant-galore.medium.com/monetizing-the-golden-age-of-short-selling-aab473122296

Data required

* daily data for the day of the tip plus 30 days of both prior and subsequent history



Project: Pre-Market Drop Risk

Main question: are these sufficiently liquid and short-sellable via IG Markets?

Article at https://quant-galore.medium.com/monetizing-the-golden-age-of-short-selling-aab473122296

Data required

* daily and 5m data for the day of the trade and the 20 prior days



Project: Dilutive S-1 Filings

Main question: are these sufficiently liquid and short-sellable via IG Markets?

Article at https://quant-galore.medium.com/monetizing-the-golden-age-of-short-selling-aab473122296

Data required:

* one year of prior daily data
* 30 days of subsequent daily data
* SEC Filing Intensity and Historical Market Cap 







Other Alphanume datasets

Wikipedia Views https://quant-galore.medium.com/the-quant-traders-guide-to-the-alphanume-market-data-api-62adffe1972d (add value to QGMI and SDA)

S&P500 risk regime https://quant-galore.medium.com/the-quant-traders-guide-to-the-alphanume-market-data-api-62adffe1972d (add value to various things, see how SDA's traffic lights compare to this.  Also use this instead of beta when constructing an ersatz QGMI index)

Historical Optional Tickers https://quant-galore.medium.com/the-quant-traders-guide-to-the-alphanume-market-data-api-62adffe1972d (build an index)

SEC Filing Intensity https://quant-galore.medium.com/the-quant-traders-guide-to-the-alphanume-market-data-api-62adffe1972d (Add value to QGMI and SDA, behaviour of stock may also depend on market cap, so should be accompanied with Historical Market Cap dataset)

Ticker Classification https://quant-galore.medium.com/the-quant-traders-guide-to-the-alphanume-market-data-api-62adffe1972d (find the strongest stock in the strongest sector)

Historical Market Cap https://quant-galore.medium.com/the-quant-traders-guide-to-the-alphanume-market-data-api-62adffe1972d (may be needed to get sense out of SEC Filing Intensity as filing implies different things for different market caps)



For the three short selling projects, can we easily track dividend payments?



Also, Alphanume have a direct-to-LLM connection, described at https://www.quantconnect.com/announcements/19439/quantconnect-mcp-server/p1