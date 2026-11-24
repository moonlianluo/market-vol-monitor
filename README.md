# Market Vol Monitor

[![tests](https://github.com/moonlianluo/market-vol-monitor/actions/workflows/test.yml/badge.svg)](https://github.com/moonlianluo/market-vol-monitor/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-dashboard-FF4B4B)](https://streamlit.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An interactive research dashboard for monitoring realized volatility across the
U.S. Treasury curve, broad equity benchmarks, and the largest technology stocks.

The project combines official Treasury par yields with public equity price
history, calculates comparable rolling volatility measures, and surfaces
cross-asset regime changes in a single dashboard.

## What it answers

- Where is realized volatility concentrated along the Treasury curve?
- How do 1-month, 3-month, and 1-year windows differ?
- Is equity volatility concentrated in mega-cap technology or broad markets?
- Are rates or equities carrying more cross-asset stress?
- Is the current environment historically Low, Normal, Elevated, or Stress?
- Is stock–Treasury correlation helping or hurting diversification?

## Dashboard sections

| Section | Contents |
|---|---|
| Rates | Term structure, rolling volatility, yield levels, curve snapshots |
| Equities | Benchmark and single-name realized volatility, indexed performance |
| Cross-asset | 10Y Treasury volatility versus SPY volatility |
| Regime | Historical percentile, log-vol z-score, stock–bond correlation |

## Architecture

```text
Treasury.gov ───────────────┐
                           ├─ fetch/cache ─ analytics ─ JSON payload
yfinance ──┐               │                              │
           ├─ fallback ────┘                              ▼
Stooq ─────┘                                      dashboard renderer
                                                         │
                                      ┌──────────────────┴──────────────┐
                                      ▼                                 ▼
                                Streamlit app                 standalone HTML
```

## Methodology

Rates volatility is the annualized standard deviation of daily yield changes,
reported in basis points. Equity volatility is the annualized standard deviation
of daily log returns, reported in percent:

```text
rates vol  = std(daily yield change) × √252 × 100
equity vol = std(daily log return)   × √252 × 100
```

The current 3-month observation is classified against its available historical
distribution. See [the methodology note](docs/methodology.md) for definitions,
source details, and limitations.

## Data sources and freshness

- **Treasury rates:** U.S. Treasury daily par yield curve CSVs
- **Equities:** yfinance adjusted closes, with Stooq fallback
- **Caching:** current data is cached for one hour in Streamlit
- **Daily bars:** a still-forming U.S. equity candle is excluded

The dashboard displays its as-of dates. Provider histories may differ because of
adjustments, symbol availability, and publication timing.

## Run locally

```powershell
python -m venv .venv
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Standalone and cached-data workflows:

```powershell
python update_dashboard.py
python update_dashboard.py --serve
python update_dashboard.py --offline
```

## Test

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m py_compile streamlit_app.py update_dashboard.py
```

Tests use deterministic synthetic histories and do not call live data services.

## Deploy

Create a Streamlit Community Cloud app using `streamlit_app.py` as the entry
point. No API key is required; fresh builds need access to the public providers.

## Scope

This is a public research and visualization project. It is not a trading system,
volatility forecast, official market-data source, or investment recommendation.

## License

[MIT](LICENSE).
