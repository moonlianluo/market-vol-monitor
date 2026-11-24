# Methodology

## Rates volatility

For each Treasury tenor, the monitor calculates daily changes in the par yield
and reports the sample standard deviation over 21-, 63-, and 252-observation
windows:

\[
\sigma_{rates} = SD(\Delta y_t) \times \sqrt{252} \times 100
\]

Yields are expressed in percentage points, so multiplying by 100 reports the
annualized result in basis points.

## Equity volatility

Equity realized volatility uses daily log returns:

\[
r_t = \log(P_t) - \log(P_{t-1})
\]

\[
\sigma_{equity} = SD(r_t) \times \sqrt{252} \times 100
\]

The result is annualized percent volatility. A still-forming current-day candle
is removed before calculation.

## Regime classification

The current 63-observation volatility is compared with its available history:

- Low: below the 25th percentile
- Normal: 25th–75th percentile
- Elevated: 75th–90th percentile
- Stress: above the 90th percentile

Rates use the average of 2-year and 10-year realized volatility. Equity uses SPY.
The percentile is primary; a log-volatility z-score is shown as context.

## Data sources

- U.S. Treasury daily par yield curve rates: Treasury.gov
- Adjusted equity closes: yfinance, with Stooq as a fallback

Providers can differ in adjustment methodology, publication timing, symbol
history, and holiday handling. This is a research monitor, not an official
valuation or trading system.

## Limitations

- Realized volatility is backward-looking.
- Square-root-of-time annualization assumes a stable observation frequency.
- Missing observations and provider histories can change available samples.
- Regime percentiles depend on the history available for each series.
- No forecast, recommendation, or suitability assessment is produced.

