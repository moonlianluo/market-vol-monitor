#!/usr/bin/env python3
"""
Market Vol Monitor — self-updating dashboard generator.

Pulls U.S. Treasury daily par yields (treasury.gov) and equity prices
(stooq.com, free, no API key), computes annualized volatilities, and
writes a single-file dashboard.html.

Usage:
    python update_dashboard.py                       # fetch fresh data, rebuild dashboard.html
    python update_dashboard.py --offline             # rebuild from cached data only
    python update_dashboard.py --bootstrap-xlsx F    # seed the Treasury cache from the old workbook

Dependencies: pandas, numpy, requests   (pip install pandas numpy requests)

Data is cached in ./data/ as plain CSVs:
    treasury_<year>.csv   — one per calendar year (current year is always re-fetched)
    equity_<ticker>.csv   — full daily close history per ticker
    tariffs.csv           — annual average U.S. tariff rate; append a row each year by hand

Run it on a schedule (cron / Windows Task Scheduler) and point people at dashboard.html.
"""

import argparse
import io
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------- config

DATA_DIR = Path(__file__).parent / "data"
OUT_HTML = Path(__file__).parent / "dashboard.html"

TENORS = ['1 Mo', '2 Mo', '3 Mo', '6 Mo', '1 Yr', '2 Yr', '3 Yr',
          '5 Yr', '7 Yr', '10 Yr', '20 Yr', '30 Yr']
TS_TENORS = ['3 Mo', '2 Yr', '5 Yr', '10 Yr', '30 Yr']   # tenors charted over time
FIRST_YEAR = 1990

# label -> (start, end) calendar spans, inclusive
REGIMES = {
    "long": ("2005", "2024"),   # long-run average
    "low":  ("2012", "2021"),   # low-rate, low-vol period
    "high": ("2022", "2023"),   # hiking-cycle high-vol period
}

WINDOWS = {"1M": 21, "3M": 63, "1Y": 252}

# equity universe: benchmarks first, then the Mag 7
BENCH = ['MAGS', 'QQQ', 'SPY']
MAG7 = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA']
TICKERS = BENCH + MAG7

ANN_RATES = np.sqrt(252) * 100          # daily yield-change std (pct) -> annualized bp
ANN_EQ = np.sqrt(252) * 100             # daily log-return std -> annualized %

TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/daily-treasury-rates.csv/{year}/all"
                "?type=daily_treasury_yield_curve&field_tdr_date_value={year}"
                "&page&_format=csv")
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"

# Annual average U.S. tariff rate on total imports (%). Seeds data/tariffs.csv on
# first run; edit/append that file by hand thereafter — it wins over this default.
TARIFF_DEFAULT = "Year,Rate\n1900,27.5\n1901,28.8\n1902,27.8\n1903,27.8\n1904,26.2\n1905,23.7\n1906,24.2\n1907,23.3\n1908,23.9\n1909,23.0\n1910,21.1\n1911,20.3\n1912,18.6\n1913,17.7\n1914,14.9\n1915,12.5\n1916,9.6\n1917,6.3\n1918,5.7\n1919,6.2\n1920,6.4\n1921,11.4\n1922,14.7\n1923,15.2\n1924,14.9\n1925,13.2\n1926,13.4\n1927,13.8\n1928,13.3\n1929,13.5\n1930,15.8\n1931,17.8\n1932,19.6\n1933,19.8\n1934,18.4\n1935,17.5\n1936,16.8\n1937,15.6\n1938,15.5\n1939,14.4\n1940,12.5\n1941,13.6\n1942,11.6\n1943,11.6\n1944,9.9\n1945,9.6\n1946,10.3\n1947,7.9\n1948,5.9\n1949,5.7\n1950,6.1\n1951,5.6\n1952,5.3\n1953,5.5\n1954,5.4\n1955,5.9\n1956,5.9\n1957,6.0\n1958,6.5\n1959,7.1\n1960,7.2\n1961,7.2\n1962,7.6\n1963,7.4\n1964,7.4\n1965,7.6\n1966,7.6\n1967,7.5\n1968,7.1\n1969,7.1\n1970,6.5\n1971,6.1\n1972,5.7\n1973,5.0\n1974,3.8\n1975,3.9\n1976,3.9\n1977,4.0\n1978,3.5\n1979,3.1\n1980,3.4\n1981,3.6\n1982,3.7\n1983,3.7\n1984,3.8\n1985,3.6\n1986,3.5\n1987,3.4\n1988,3.4\n1989,3.3\n1990,3.4\n1991,3.3\n1992,3.2\n1993,3.0\n1994,2.5\n1995,2.3\n1996,2.1\n1997,2.0\n1998,1.8\n1999,1.6\n2000,1.6\n2001,1.7\n2002,1.6\n2003,1.5\n2004,1.4\n2005,1.4\n2006,1.4\n2007,1.3\n2008,1.4\n2009,1.4\n2010,1.3\n2011,1.3\n2012,1.4\n2013,1.4\n2014,1.5\n2015,1.5\n2016,1.4\n2017,1.8\n2018,2.7\n2019,2.8\n2020,2.8\n2021,3.0\n2022,2.8\n2023,2.4\n2024,2.5\n2025,14.5\n"

# ----------------------------------------------------------------------------- fetch

def _http_get(url: str) -> str:
    import requests
    r = requests.get(url, timeout=60, headers={"User-Agent": "vol-dashboard/1.0"})
    r.raise_for_status()
    return r.text


def load_treasury(offline: bool) -> pd.DataFrame:
    """Daily par yields, one row per business day, columns = TENORS."""
    DATA_DIR.mkdir(exist_ok=True)
    this_year = date.today().year
    frames = []
    for year in range(FIRST_YEAR, this_year + 1):
        cached = DATA_DIR / f"treasury_{year}.csv"
        need_fetch = (not cached.exists()) or (year == this_year)
        if need_fetch and not offline:
            try:
                text = _http_get(TREASURY_URL.format(year=year))
                pd.read_csv(io.StringIO(text))          # validate before writing
                cached.write_text(text, encoding='utf-8')
                print(f"  treasury {year}: fetched")
            except Exception as e:
                if cached.exists():
                    print(f"  treasury {year}: fetch failed ({e}); using cache")
                else:
                    print(f"  treasury {year}: fetch failed ({e}); skipping")
                    continue
        if cached.exists():
            frames.append(pd.read_csv(cached))
    if not frames:
        sys.exit("No Treasury data available. Run without --offline, or bootstrap "
                 "the cache with --bootstrap-xlsx <workbook.xlsx>.")
    df = pd.concat(frames, ignore_index=True)
    df['Date'] = pd.to_datetime(df['Date'], format='mixed')
    keep = ['Date'] + [c for c in TENORS if c in df.columns]
    df = (df[keep].sort_values('Date').drop_duplicates('Date')
          .set_index('Date').apply(pd.to_numeric, errors='coerce'))
    return df


def bootstrap_from_xlsx(path: str) -> None:
    """Seed the Treasury cache from the legacy workbook (first sheet)."""
    DATA_DIR.mkdir(exist_ok=True)
    raw = pd.read_excel(path, sheet_name=0, header=None)
    hdr = raw.index[raw[0] == 'Date'][0]
    df = pd.read_excel(path, sheet_name=0, header=hdr, usecols="A:M")
    df.columns = ['Date'] + TENORS
    df = df.dropna(subset=['Date'])
    df['Date'] = pd.to_datetime(df['Date'])
    for year, grp in df.groupby(df['Date'].dt.year):
        f = DATA_DIR / f"treasury_{year}.csv"
        if not f.exists():
            grp.sort_values('Date').to_csv(f, index=False)
    print(f"Bootstrapped Treasury cache: {df['Date'].min().date()} -> {df['Date'].max().date()}")


def _fetch_stooq(sym: str) -> pd.DataFrame:
    text = _http_get(STOOQ_URL.format(sym=sym.lower()))
    s = pd.read_csv(io.StringIO(text))
    if 'Close' not in s.columns or len(s) < 30:
        raise ValueError("unexpected Stooq response")
    return _completed_equity_bars(s[['Date', 'Close']])


def _fetch_yfinance(sym: str) -> pd.DataFrame:
    import yfinance as yf                      # optional: pip install yfinance
    h = yf.Ticker(sym).history(period="max", auto_adjust=True)
    if h.empty or 'Close' not in h.columns:
        raise ValueError("empty yfinance response")
    s = h['Close'].copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    out = s.rename('Close').reset_index().rename(columns={'index': 'Date'})
    return _completed_equity_bars(out)


def _last_completed_equity_date() -> date:
    """Latest date that can safely be treated as a completed US daily bar."""
    now = datetime.now(ZoneInfo("America/New_York"))
    # Allow a short stabilization delay after the 4:00pm cash close. On a
    # weekday before then, today's yfinance daily bar is still intraday.
    if now.weekday() < 5 and now.time() < time(16, 15):
        return now.date() - timedelta(days=1)
    return now.date()


def _completed_equity_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Remove a provider's still-forming current daily candle."""
    out = df.copy()
    out['Date'] = pd.to_datetime(out['Date']).dt.tz_localize(None)
    cutoff = pd.Timestamp(_last_completed_equity_date())
    return out.loc[out['Date'] <= cutoff, ['Date', 'Close']]


def load_equity(offline: bool) -> pd.DataFrame | None:
    """Daily closes, columns = TICKERS. Tries yfinance first (if installed),
    then Stooq, then the local cache. Returns None if nothing is available."""
    DATA_DIR.mkdir(exist_ok=True)
    try:
        import yfinance  # noqa: F401
        sources = [("yfinance", _fetch_yfinance), ("stooq", _fetch_stooq)]
    except ImportError:
        sources = [("stooq", _fetch_stooq)]

    cols = {}
    last_complete = pd.Timestamp(_last_completed_equity_date())
    for sym in TICKERS:
        cached = DATA_DIR / f"equity_{sym}.csv"
        if not offline:
            for name, fetch in sources:
                try:
                    df = fetch(sym)
                    df.to_csv(cached, index=False)
                    print(f"  equity {sym}: fetched via {name} ({len(df)} rows)")
                    break
                except Exception as e:
                    print(f"  equity {sym}: {name} failed ({e})")
            else:
                print(f"  equity {sym}: all sources failed"
                      + ("; using cache" if cached.exists() else "; skipping"))
        if cached.exists():
            s = pd.read_csv(cached, parse_dates=['Date']).set_index('Date')['Close']
            s = pd.to_numeric(s, errors='coerce')
            cols[sym] = s.loc[s.index <= last_complete]
    if not cols:
        return None
    return pd.DataFrame(cols).sort_index()


def load_tariffs() -> pd.DataFrame:
    DATA_DIR.mkdir(exist_ok=True)
    f = DATA_DIR / "tariffs.csv"
    if not f.exists():
        f.write_text(TARIFF_DEFAULT, encoding='utf-8')
    t = pd.read_csv(f).dropna()
    t.columns = ['Year', 'Rate']
    return t

# ----------------------------------------------------------------------------- compute

def _vol(x: pd.Series, ann: float, min_obs: int = 16) -> float | None:
    x = x.dropna()
    if len(x) < min_obs:
        return None
    return round(float(x.std(ddof=1) * ann), 1)


def _weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Thin daily observations while always retaining the latest row."""
    sampled = df.iloc[::5]
    if len(df) and (sampled.empty or sampled.index[-1] != df.index[-1]):
        sampled = pd.concat([sampled, df.iloc[[-1]]])
    return sampled


def compute_rates(rates: pd.DataFrame) -> dict:
    chg = rates[TENORS].diff()
    term = {"tenors": TENORS}
    for w, n in WINDOWS.items():
        term[w] = [_vol(chg[t].tail(n), ANN_RATES) for t in TENORS]
    n3 = WINDOWS["3M"]
    pctile = []
    for t in TENORS:
        hist = (chg[t].rolling(n3, min_periods=int(n3 * .8)).std(ddof=1) * ANN_RATES).dropna()
        pctile.append(round(float((hist < hist.iloc[-1]).mean() * 100), 0) if len(hist) > 260 else None)
    term["pctile"] = pctile
    def _regime_segment(a: str, b: str) -> pd.DataFrame:
        # Never label a partial historical interval as the complete regime.
        complete = rates.index[-1] >= pd.Timestamp(f"{b}-12-01")
        return chg.loc[a:b] if complete else chg.iloc[0:0]

    for key, (a, b) in REGIMES.items():
        seg = _regime_segment(a, b)
        term[key] = [_vol(seg[t], ANN_RATES, min_obs=60) for t in TENORS]

    roll, dates = {}, None
    for w, n in WINDOWS.items():
        r = chg[TS_TENORS].rolling(n, min_periods=int(n * 0.8)).std(ddof=1) * ANN_RATES
        r = _weekly(r).round(1)
        roll[w] = {t: [None if pd.isna(v) else float(v) for v in r[t]] for t in TS_TENORS}
        dates = [d.strftime('%Y-%m-%d') for d in r.index]

    ref = {key: {t: _vol(_regime_segment(a, b)[t], ANN_RATES, 60) for t in TS_TENORS}
           for key, (a, b) in REGIMES.items()}

    # --- level data: yield history (weekly, same sampling as vol series) ---
    lvl = _weekly(rates[TS_TENORS]).round(2)
    lvl_series = {t: [None if pd.isna(v) else float(v) for v in lvl[t]] for t in TS_TENORS}

    # --- level data: curve snapshots (today vs ~1M and ~1Y of trading days ago) ---
    def _snap(off):
        if len(rates) <= off:
            return None
        row = rates.iloc[-1 - off]
        return {"date": row.name.strftime('%b %d, %Y'),
                "vals": [None if pd.isna(row[t]) else round(float(row[t]), 2) for t in TENORS]}
    curve = {"now": _snap(0), "m1": _snap(21), "y1": _snap(252)}

    return {
        "asOf": rates.index[-1].strftime('%B %d, %Y'),
        "lastRates": {t: (None if pd.isna(v) else round(float(v), 2))
                      for t, v in rates.iloc[-1].items()},
        "term": term, "tsDates": dates, "tsSeries": roll,
        "tsTenors": TS_TENORS, "ref": ref,
        "lvlSeries": lvl_series, "curve": curve,
    }


def compute_equity(px: pd.DataFrame | None) -> dict | None:
    if px is None or px.empty:
        return None
    ret = np.log(px).diff()
    # A column may exist while containing no observations before a historical
    # cutoff (for example SPY before its launch). Exclude those empty series.
    have = [s for s in TICKERS if s in ret.columns and px[s].notna().any()]

    table = {}
    for s in have:
        row = {w: _vol(ret[s].tail(n), ANN_EQ) for w, n in WINDOWS.items()}
        last = px[s].dropna()
        row["last"] = round(float(last.iloc[-1]), 2) if len(last) else None
        # YTD return, %
        ytd = px[s].dropna()
        ytd = ytd[ytd.index.year == ytd.index[-1].year]
        row["ytd"] = round((float(ytd.iloc[-1]) / float(ytd.iloc[0]) - 1) * 100, 1) if len(ytd) > 1 else None
        table[s] = row

    bench = [s for s in BENCH if s in have]
    mag7 = [s for s in MAG7 if s in have]
    chart_names = bench + mag7
    roll, dates = {}, None
    since = ret.index >= "2000-01-01"
    for w, n in WINDOWS.items():
        r = ret.loc[since, chart_names].rolling(n, min_periods=int(n * 0.8)).std(ddof=1) * ANN_EQ
        r = _weekly(r).round(1)
        roll[w] = {s: [None if pd.isna(v) else float(v) for v in r[s]] for s in chart_names}
        dates = [d.strftime('%Y-%m-%d') for d in r.index]

    # level data: weekly closes for the benchmarks (same sampling / dates)
    pw = _weekly(px.loc[since, bench]).round(2)
    px_series = {s: [None if pd.isna(v) else float(v) for v in pw[s]] for s in bench}

    return {
        "asOf": px[have].dropna(how='all').index[-1].strftime('%B %d, %Y'),
        "bench": bench, "mag7": mag7,
        "table": table, "tsDates": dates, "tsSeries": roll,
        "pxSeries": px_series,
    }


def compute_cross(rates: pd.DataFrame, px: pd.DataFrame | None) -> dict | None:
    """Rolling 3M vol: 10Y Treasury (bp) vs SPY (%), weekly, since 2000."""
    if px is None or 'SPY' not in px.columns or '10 Yr' not in rates.columns:
        return None
    n = WINDOWS["3M"]
    ust = rates['10 Yr'].diff().rolling(n, min_periods=int(n * .8)).std(ddof=1) * ANN_RATES
    spy = np.log(px['SPY']).diff().rolling(n, min_periods=int(n * .8)).std(ddof=1) * ANN_EQ
    both = pd.DataFrame({"ust": ust, "spy": spy}).loc["2000-01-01":].dropna(how='all')
    both = _weekly(both).round(1)
    if both.empty:
        return None
    return {
        "dates": [d.strftime('%Y-%m-%d') for d in both.index],
        "ust": [None if pd.isna(v) else float(v) for v in both["ust"]],
        "spy": [None if pd.isna(v) else float(v) for v in both["spy"]],
    }


# --- regime classification -----------------------------------------------
# Metric: rolling 3M vol. Rates composite = average of 2Y and 10Y bp vol
# (front-end policy risk + duration benchmark; bills excluded because
# debt-ceiling noise distorts them). Classification = full-sample percentile
# of the current reading: Low < 25th, Normal 25–75, Elevated 75–90,
# Stress > 90th. Percentile is primary because vol is right-skewed; the
# z-score is computed on log-vol (closer to normal) and shown as context.

REGIME_BANDS = [(25, "Low"), (75, "Normal"), (90, "Elevated"), (100, "Stress")]


def _classify(series: pd.Series) -> dict | None:
    s = series.dropna()
    if len(s) < 260:
        return None
    cur = float(s.iloc[-1])
    pct = float((s < cur).mean() * 100)
    logs = np.log(s)
    z = float((np.log(cur) - logs.mean()) / logs.std(ddof=1))
    label = next(lab for hi, lab in REGIME_BANDS if pct <= hi)
    # bucket code per weekly observation, for the timeline strip
    ranks = s.rank(pct=True) * 100
    codes = pd.Series(np.searchsorted([25, 75, 90], ranks, side='right'), index=s.index)
    wk = _weekly(codes.to_frame('c'))['c']
    return {
        "label": label, "pct": round(pct, 1), "z": round(z, 2),
        "cur": round(cur, 1), "median": round(float(s.median()), 1),
        "stripDates": [d.strftime('%Y-%m-%d') for d in wk.index],
        "strip": [int(v) for v in wk],
    }


def compute_regime(rates: pd.DataFrame, px: pd.DataFrame | None) -> dict:
    n = WINDOWS["3M"]
    comp = (rates[['2 Yr', '10 Yr']].diff()
            .rolling(n, min_periods=int(n * .8)).std(ddof=1) * ANN_RATES).mean(axis=1)
    out = {"rates": _classify(comp), "eq": None, "corr": None}

    if px is not None and 'SPY' in px.columns:
        spy_vol = (np.log(px['SPY']).diff()
                   .rolling(n, min_periods=int(n * .8)).std(ddof=1) * ANN_EQ)
        out["eq"] = _classify(spy_vol.loc["2000-01-01":])

        # rolling 1Y stock–bond return correlation:
        # SPY daily log returns vs −Δ10Y yield (proxy for Treasury returns)
        pair = pd.DataFrame({
            "spy": np.log(px['SPY']).diff(),
            "bond": -rates['10 Yr'].diff(),
        }).dropna()
        corr = pair['spy'].rolling(252, min_periods=200).corr(pair['bond'])
        corr = _weekly(corr.loc["2000-01-01":].to_frame('c'))['c'].round(2)
        corr = corr.dropna()
        if len(corr):
            out["corr"] = {
                "dates": [d.strftime('%Y-%m-%d') for d in corr.index],
                "vals": [float(v) for v in corr],
            }
    return out

# ----------------------------------------------------------------------------- render

def render_html(payload: dict) -> str:
    return TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, separators=(',', ':')))


def render(payload: dict) -> None:
    html = render_html(payload)
    OUT_HTML.write_text(html, encoding='utf-8')
    print(f"Wrote {OUT_HTML}  ({OUT_HTML.stat().st_size/1024:.0f} KB)")


def make_payload(offline: bool, as_of: str | None = None) -> dict:
    """Load and compute dashboard data without changing dashboard.html."""
    print("Loading Treasury yields...")
    rates = load_treasury(offline)
    try:
        cutoff = pd.Timestamp(as_of).normalize() if as_of else None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid as-of date: {as_of}") from exc
    if cutoff is not None and cutoff > pd.Timestamp(date.today()):
        raise ValueError("as-of date cannot be in the future")
    if cutoff is not None:
        rates = rates.loc[:cutoff]
        if rates.empty:
            raise ValueError(f"no Treasury data on or before {as_of}")
    print(f"  {rates.index[0].date()} -> {rates.index[-1].date()}  ({len(rates)} days)")

    print("Loading equity prices...")
    px = load_equity(offline)
    if px is not None and cutoff is not None:
        px = px.loc[:cutoff]
        if px.empty:
            px = None
    if px is None:
        print("  no equity data available - equity sections will show an empty state")

    tariffs = load_tariffs()
    if cutoff is not None:
        tariffs = tariffs[tariffs['Year'] <= cutoff.year]

    payload = {
        "generated": date.today().strftime('%B %d, %Y'),
        "selectedAsOf": cutoff.strftime('%Y-%m-%d') if cutoff is not None else None,
        "rates": compute_rates(rates),
        "eq": compute_equity(px),
        "cross": compute_cross(rates, px),
        "regime": compute_regime(rates, px),
        "tariffs": {"years": tariffs['Year'].astype(int).tolist(),
                    "rates": tariffs['Rate'].astype(float).round(2).tolist()},
    }
    return payload


def build(offline: bool, as_of: str | None = None) -> dict:
    """Fetch, compute, and write the latest dashboard to disk."""
    payload = make_payload(offline, as_of)
    render(payload)
    return {"ok": True, "ratesAsOf": payload["rates"]["asOf"],
            "eqAsOf": payload["eq"]["asOf"] if payload["eq"] else None}


def serve(port: int) -> None:
    """Serve the dashboard locally with a working /refresh endpoint."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    refresh_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(self.path)
            if parsed.path in ('/', '/dashboard.html', '/index.html'):
                chosen = parse_qs(parsed.query).get('asof', [None])[0]
                if not chosen:
                    self._html(200, OUT_HTML.read_bytes())
                    return
                if not refresh_lock.acquire(blocking=False):
                    self._json(409, {"ok": False, "error": "dashboard build already running"})
                    return
                try:
                    # Historical views are rendered per request and never replace
                    # the latest shared dashboard.html.
                    body = render_html(make_payload(offline=True, as_of=chosen)).encode('utf-8')
                    self._html(200, body)
                except Exception as e:
                    self._json(400, {"ok": False, "error": str(e)})
                finally:
                    refresh_lock.release()
            elif parsed.path == '/refresh':
                if not refresh_lock.acquire(blocking=False):
                    self._json(409, {"ok": False, "error": "refresh already running"})
                    return
                try:
                    self._json(200, build(offline=False))
                except Exception as e:  # keep serving even if a source is down
                    self._json(500, {"ok": False, "error": str(e)})
                finally:
                    refresh_lock.release()
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def log_message(self, fmt, *args):  # quieter logs
            print(f"  [{self.address_string()}] {fmt % args}")

    if not OUT_HTML.exists():
        build(offline=False)
    print(f"\nServing dashboard at http://localhost:{port}  (Ctrl-C to stop)")
    print("The page's Refresh button re-pulls all data; stale data auto-refreshes on open.")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild the market vol dashboard.")
    ap.add_argument("--offline", action="store_true", help="use cached data only")
    ap.add_argument("--bootstrap-xlsx", metavar="FILE",
                    help="seed the Treasury cache from the legacy workbook")
    ap.add_argument("--serve", action="store_true",
                    help="serve the dashboard on a local web server with a live Refresh button")
    ap.add_argument("--port", type=int, default=8000, help="port for --serve (default 8000)")
    args = ap.parse_args()

    if args.bootstrap_xlsx:
        bootstrap_from_xlsx(args.bootstrap_xlsx)

    if args.serve:
        serve(args.port)
    else:
        build(args.offline)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Market Vol Monitor</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{
    --ink:#17293e; --ink-2:#3c5165; --muted:#6d7c8b;
    --paper:#f2f4f6; --card:#ffffff; --line:#dde3e9;
    --w1m:#8fb3d9; --w3m:#3a6ea5; --w1y:#17293e;
    --lo:#1e7f74; --hi:#c75b12; --lr:#8a94a0;
    font-size:15px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"Segoe UI",system-ui,-apple-system,Helvetica,Arial,sans-serif;
    background:var(--paper);color:var(--ink);-webkit-font-smoothing:antialiased}
  .mono{font-family:"Consolas","SF Mono","Menlo",monospace}
  .wrap{max-width:1180px;margin:0 auto;padding:0 24px 56px}

  header{border-bottom:2px solid var(--ink);padding:26px 0 18px;margin-bottom:26px}
  .eyebrow{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
  h1{font-size:1.7rem;font-weight:700;letter-spacing:-.01em}
  h1 small{font-weight:400;color:var(--ink-2)}
  .asof{text-align:left;font-size:.82rem;color:var(--muted);line-height:1.5;margin-top:12px}
  .asof b{color:var(--ink);font-weight:600}

  .tabs{position:sticky;top:0;z-index:20;display:flex;gap:0;background:var(--paper);
    border-bottom:2px solid var(--ink);margin-bottom:24px}
  .tab{border:none;background:transparent;font-family:inherit;cursor:pointer;
    font-size:.8rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
    padding:12px 18px 10px;border-bottom:3px solid transparent;margin-bottom:-2px;font-weight:600}
  .tab:hover{color:var(--ink-2)}
  .tab.on{color:var(--ink);border-bottom-color:var(--ink)}
  .tab:focus-visible{outline:2px solid var(--w3m);outline-offset:-2px}
  .tab-panel{display:none}
  .tab-panel.on{display:block}

  .banner{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--lr);
    padding:14px 18px;margin-bottom:14px;display:flex;gap:26px;align-items:center;flex-wrap:wrap}
  .banner .lab{font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .banner .reg{font-size:1.45rem;font-weight:700;letter-spacing:.02em}
  .banner .kv{font-size:.8rem;color:var(--ink-2);line-height:1.55}
  .banner .kv b{font-weight:700;color:var(--ink)}
  .reg-Low{color:var(--lo)} .reg-Normal{color:var(--ink)}
  .reg-Elevated{color:var(--hi)} .reg-Stress{color:#a03123}
  .bl-Low{border-left-color:var(--lo)!important} .bl-Normal{border-left-color:var(--ink)!important}
  .bl-Elevated{border-left-color:var(--hi)!important} .bl-Stress{border-left-color:#a03123!important}

  .fresh{display:inline-block;font-size:.72rem;padding:2px 9px;border:1px solid var(--line);
    margin-top:5px;color:var(--ink-2);background:var(--card)}
  .fresh.stale{border-color:var(--hi);color:var(--hi);font-weight:600}
  .refresh-btn{margin-left:8px;margin-top:5px;border:1px solid var(--ink);background:var(--ink);color:#fff;
    font-family:inherit;font-size:.72rem;font-weight:600;padding:2px 11px;cursor:pointer}
  .refresh-btn:disabled{opacity:.55;cursor:wait}
  .asof-controls{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-top:7px}
  .asof-controls select,.asof-controls input{border:1px solid var(--line);background:var(--card);
    color:var(--ink-2);font:inherit;font-size:.75rem;padding:3px 7px;height:27px}
  .dates{display:none;gap:6px;align-items:center}
  .dates.show{display:inline-flex}
  .dates input{border:1px solid var(--line);font-family:inherit;font-size:.75rem;color:var(--ink-2);
    padding:3px 6px;background:var(--card)}
  .dates span{font-size:.72rem;color:var(--muted)}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:26px}
  .card{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--w3m);padding:14px 16px 12px}
  .card .t{font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .card .v{font-size:1.9rem;font-weight:700;margin:4px 0 2px}
  .card .v span{font-size:.85rem;font-weight:500;color:var(--muted)}
  .card .d{font-size:.78rem;color:var(--ink-2)}
  .card .d .up{color:var(--hi);font-weight:600}
  .card .d .dn{color:var(--lo);font-weight:600}

  section{background:var(--card);border:1px solid var(--line);padding:20px 22px 16px;margin-bottom:26px}
  .sec-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
  h2{font-size:1.02rem;font-weight:700}
  .sub{font-size:.8rem;color:var(--muted);margin-bottom:14px}
  .chartbox{position:relative;height:400px}
  .chartbox.short{height:340px}
  .chartbox.mini{height:280px}

  .controls{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
  .seg{display:inline-flex;border:1px solid var(--line)}
  .seg button{border:none;background:transparent;padding:5px 13px;font-size:.78rem;cursor:pointer;
    color:var(--ink-2);font-family:inherit;border-right:1px solid var(--line)}
  .seg button:last-child{border-right:none}
  .seg button.on{background:var(--ink);color:#fff}
  .seg button:focus-visible{outline:2px solid var(--w3m);outline-offset:-2px}
  .chips{display:flex;gap:6px;flex-wrap:wrap}
  .chip{border:1px solid var(--line);background:transparent;padding:4px 11px;font-size:.78rem;
    cursor:pointer;color:var(--ink-2);font-family:inherit}
  .chip.on{border-color:var(--ink);background:var(--paper);color:var(--ink);font-weight:600}
  .chip:focus-visible{outline:2px solid var(--w3m);outline-offset:-2px}
  .ctl-label{font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
  .hint{font-size:.75rem;color:var(--muted);margin-top:8px}
  .swatch{display:inline-block;width:9px;height:9px;margin-right:6px;vertical-align:baseline}

  table{width:100%;border-collapse:collapse;font-size:.82rem}
  th,td{padding:7px 8px;text-align:right;border-bottom:1px solid var(--line)}
  th{font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
  th:first-child,td:first-child{text-align:left;white-space:nowrap}
  td:first-child{font-weight:600;color:var(--ink-2);font-size:.78rem}
  tr.rule td{border-bottom:2px solid var(--ink)}
  tr.regime td:first-child{font-weight:400;font-style:italic}
  td.lo-c{color:var(--lo)} td.hi-c{color:var(--hi)}

  .empty{border:1px dashed var(--line);background:var(--paper);padding:26px;text-align:center;
    font-size:.85rem;color:var(--muted);line-height:1.7}
  .empty code{font-family:"Consolas",monospace;background:#fff;border:1px solid var(--line);padding:1px 6px}

  footer{font-size:.74rem;color:var(--muted);line-height:1.6;border-top:1px solid var(--line);padding-top:14px}
  footer b{color:var(--ink-2)}
  @media (max-width:700px){.chartbox{height:320px}}
  @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <div class="eyebrow">Cross-Asset &middot; Risk Monitor</div>
      <h1>Market Vol Monitor <small>&mdash; rates, Mag 7 &amp; benchmark equity vol, policy backdrop</small></h1>
      <div class="asof">
      Rates through <b id="asofRates"></b> &middot; equities through <b id="asofEq">&mdash;</b><br>
      Rebuilt <b id="genDate"></b> &middot; Treasury.gov par yields &middot; equity daily closes (yfinance / Stooq)
      <div class="asof-controls" id="asofControls">
        <select id="dateMode" aria-label="Data date mode"><option value="latest">Up to date</option><option value="date">Choose date</option></select>
        <input id="asofDate" type="date" aria-label="As-of date" style="display:none">
        <button class="refresh-btn" id="applyDateBtn" style="display:none">Apply date</button>
        <span class="fresh" id="freshPill"></span>
        <button class="refresh-btn" id="refreshBtn" style="display:none">&#8635; Refresh data</button>
      </div>
      </div>
    </div>
  </header>

  <nav class="tabs" id="tabs" role="tablist">
    <button class="tab on" data-tab="rates" role="tab" aria-selected="true">Rates &middot; Treasury BP vol</button>
    <button class="tab" data-tab="equities" role="tab" aria-selected="false">Equities &middot; Mag 7</button>
    <button class="tab" data-tab="macro" role="tab" aria-selected="false">Cross-asset &middot; Policy</button>
  </nav>

  <div class="tab-panel on" id="panel-rates">

  <div id="ratesBanner"></div>

  <div class="cards" id="cards"></div>

  <section>
    <h2>Yield curve</h2>
    <p class="sub">Par yield levels today versus one month and one year ago (trading days).</p>
    <div class="chartbox short"><canvas id="curveChart"></canvas></div>
  </section>

  <section>
    <div class="sec-head">
      <h2>Vol term structure</h2>
      <div class="controls">
        <span class="ctl-label">Windows</span>
        <div class="chips" id="tsWindows"></div>
      </div>
    </div>
    <p class="sub">Current trailing-window vol at each curve point, plotted against the low regime (2012&ndash;2021) and high regime (2022&ndash;2023) envelope. The shaded band is the range between the two regimes; the gray dashes are the 2005&ndash;2024 average.</p>
    <div class="chartbox"><canvas id="termChart"></canvas></div>
  </section>

  <section>
    <div class="sec-head">
      <h2>Rolling vol history</h2>
      <div class="controls">
        <span class="ctl-label">Window</span>
        <div class="seg" id="winSeg"></div>
        <span class="ctl-label">Range</span>
        <div class="seg" id="rangeSeg"></div>
      </div>
    </div>
    <p class="sub" style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px">
      <span>Trailing-window annualized bp vol over time for key curve points.</span>
      <span class="chips" id="tenorChips"></span>
    </p>
    <div class="chartbox short"><canvas id="histChart"></canvas></div>
    <p class="hint">Select a single tenor to overlay its regime reference lines.</p>
  </section>

  <section>
    <div class="sec-head">
      <h2>Yield levels over time</h2>
      <div class="controls">
        <span class="ctl-label">Range</span>
        <div class="seg" id="lvlRangeSeg"></div>
      </div>
    </div>
    <p class="sub" style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px">
      <span>Daily par yields, %, weekly sampled.</span>
      <span class="chips" id="lvlChips"></span>
    </p>
    <div class="chartbox short"><canvas id="lvlChart"></canvas></div>
  </section>

  <section>
    <h2>Full grid</h2>
    <p class="sub">Annualized bp vol by tenor. Ratio row compares the current 3M window to the 2005&ndash;2024 average &mdash; below 1.00&times; means quieter than the long-run norm.</p>
    <div style="overflow-x:auto"><table id="grid"></table></div>
  </section>

  </div><!-- /panel-rates -->

  <div class="tab-panel" id="panel-equities">

  <div id="eqBanner"></div>

  <div id="eqWrap"></div>

  </div><!-- /panel-equities -->

  <div class="tab-panel" id="panel-macro">

  <section id="stripSec">
    <h2>Vol regime timeline</h2>
    <p class="sub">Each week classified by the full-sample percentile of rolling 3M vol:
      <span style="color:var(--lo);font-weight:600">Low</span> &lt;25th &middot;
      Normal 25&ndash;75th &middot;
      <span style="color:var(--hi);font-weight:600">Elevated</span> 75&ndash;90th &middot;
      <span style="color:#a03123;font-weight:600">Stress</span> &gt;90th.
      Rates row = average of 2Y and 10Y bp vol; equity row = SPY realized vol.</p>
    <canvas id="stripCanvas" style="width:100%;height:110px;display:block"></canvas>
  </section>

  <div id="corrWrap"></div>

  <div id="crossWrap"></div>

  <section>
    <h2>Average U.S. tariff rate, 1900&ndash;present</h2>
    <p class="sub">Annual average duty on total imports. Update once a year by appending a row to <span class="mono">data/tariffs.csv</span>.</p>
    <div class="chartbox mini"><canvas id="tariffChart"></canvas></div>
  </section>

  </div><!-- /panel-macro -->

  <footer>
    <b>Method.</b> Rates: BP vol = standard deviation of daily first differences in the constant-maturity par yield, annualized by &radic;252, in basis points; yield levels are Treasury par yields in percent. Equities: realized vol = standard deviation of daily log returns, annualized by &radic;252, in percent; prices via yfinance when installed, Stooq otherwise. Trailing windows use the most recent 21 (1M), 63 (3M), and 252 (1Y) trading days. Rates regime references: 2005&ndash;2024 (long-run average), 2012&ndash;2021 (low-vol), 2022&ndash;2023 (high-vol). 1&nbsp;Mo and 2&nbsp;Mo bills can spike around debt-ceiling episodes; MAGS (Roundhill Magnificent Seven ETF) launched April&nbsp;2023, so its 1Y window is its full meaningful history. Regenerate with <span class="mono">python update_dashboard.py</span>.
  </footer>

</div>

<script id="DATA" type="application/json">__PAYLOAD__</script>
<script>
const P = JSON.parse(document.getElementById('DATA').textContent);
if(typeof Chart==='undefined'){
  document.body.innerHTML='<div style="max-width:760px;margin:60px auto;padding:24px;font:16px system-ui;color:#17293e;background:white;border:1px solid #dde3e9"><b>Charts could not load.</b><br><br>This dashboard needs network access to load Chart.js. Check the connection and reload the page.</div>';
  throw new Error('Chart.js failed to load');
}
const R = P.rates, EQ = P.eq, XA = P.cross;
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const C = {w1m:css('--w1m'), w3m:css('--w3m'), w1y:css('--w1y'), lo:css('--lo'), hi:css('--hi'),
           lr:css('--lr'), ink:css('--ink'), line:css('--line'), muted:css('--muted')};
Chart.defaults.font.family = '"Segoe UI",system-ui,sans-serif';
Chart.defaults.color = C.muted;

document.getElementById('asofRates').textContent = R.asOf;
document.getElementById('genDate').textContent = P.generated;
if (EQ) document.getElementById('asofEq').textContent = EQ.asOf;

const fmt = (v,d=1) => v==null ? '—' : v.toFixed(d);
const yearTicks = rangeYears => function(v,i){
  const lab=this.getLabelForValue(v), prev=i>0?this.getLabelForValue(v-1):'';
  if(rangeYears===3){
    const m=lab.slice(0,7);
    return (m!==prev.slice(0,7) && ['01','04','07','10'].includes(lab.slice(5,7))) ? m : null;
  }
  const yr=lab.slice(0,4);
  if(yr===prev.slice(0,4)) return null;
  return (!rangeYears && +yr%5!==0) ? null : yr;
};

/* range = 0 (full) | number of years | {from,to} custom */
function sliceRange(dates, rg){
  if(!rg) return [0, dates.length];
  if(typeof rg==='number'){
    const c=new Date(dates[dates.length-1]); c.setFullYear(c.getFullYear()-rg);
    const iso=c.toISOString().slice(0,10);
    const i=dates.findIndex(d=>d>=iso);
    return [i<0?0:i, dates.length];
  }
  let i0=0, i1=dates.length;
  if(rg.from){const i=dates.findIndex(d=>d>=rg.from); i0=i<0?dates.length:i;}
  if(rg.to){const j=dates.findIndex(d=>d>rg.to); if(j>=0)i1=j;}
  return [i0, Math.max(i1,i0)];
}
function effYears(dates, rg){          // tick density for yearTicks
  if(!rg) return 0;
  if(typeof rg==='number') return rg;
  const [i0,i1]=sliceRange(dates,rg);
  if(i1<=i0) return 3;
  const span=(new Date(dates[i1-1])-new Date(dates[i0]))/3.156e10;
  return span<=4?3:(span<=15?10:0);
}
/* builds 3Y / 10Y / Full / Custom buttons + from/to date pickers */
function addRangeControl(segEl, dates, initial, onChange){
  let rg=initial;
  const custom=document.createElement('span'); custom.className='dates';
  const from=document.createElement('input'), to=document.createElement('input');
  from.type=to.type='date';
  from.min=to.min=dates[0]; from.max=to.max=dates[dates.length-1];
  const arrow=document.createElement('span'); arrow.textContent='→';
  custom.append(from,arrow,to);
  const btns=[];
  const setOn=b=>{btns.forEach(x=>x.classList.remove('on')); b.classList.add('on');};
  [['3Y',3],['10Y',10],['Full',0]].forEach(([lab,yr])=>{
    const b=document.createElement('button'); b.textContent=lab;
    if(yr===initial)b.classList.add('on');
    b.onclick=()=>{rg=yr; setOn(b); custom.classList.remove('show'); onChange(rg);};
    segEl.appendChild(b); btns.push(b);
  });
  const cb=document.createElement('button'); cb.textContent='Custom';
  cb.onclick=()=>{setOn(cb); custom.classList.add('show');
    if(!from.value){const d=new Date(dates[dates.length-1]); d.setFullYear(d.getFullYear()-1);
      from.value=d.toISOString().slice(0,10); to.value=dates[dates.length-1];}
    apply();};
  segEl.appendChild(cb); btns.push(cb);
  segEl.insertAdjacentElement('afterend', custom);
  function apply(){ rg={from:from.value||null, to:to.value||null}; onChange(rg); }
  from.onchange=to.onchange=apply;
  return ()=>rg;
}
const baseOpts = () => ({
  responsive:true, maintainAspectRatio:false, animation:{duration:200},
  interaction:{mode:'index', intersect:false},
  plugins:{legend:{position:'bottom', labels:{boxWidth:22, boxHeight:2, padding:16, font:{size:11.5}}}},
});

/* ================= RATES ================= */

const cardTenors = ['3 Mo','2 Yr','10 Yr','30 Yr'];
const cardsEl = document.getElementById('cards');
cardTenors.forEach(t=>{
  const i = R.term.tenors.indexOf(t);
  const cur = R.term['3M'][i], lr = R.term.long[i];
  if(cur==null||lr==null) return;
  const ratio = cur/lr, dir = ratio>=1?'up':'dn', pct = Math.abs(ratio-1)*100;
  cardsEl.insertAdjacentHTML('beforeend', `
    <div class="card">
      <div class="t">${t} &middot; 3M window</div>
      <div class="v mono">${cur.toFixed(0)} <span>bp/yr</span></div>
      <div class="d"><span class="${dir}">${ratio.toFixed(2)}&times;</span> long-run average
        &middot; ${pct.toFixed(0)}% ${dir==='up'?'above':'below'} &middot; yield ${fmt(R.lastRates[t],2)}%</div>
    </div>`);
});

const termWindowState = {'1M':false,'3M':true,'1Y':true};
const winColors = {'1M':C.w1m,'3M':C.w3m,'1Y':C.w1y};
function buildTerm(){
  const ds=[];
  ds.push({label:'2012–2021 low regime', data:R.term.low, borderColor:C.lo, borderDash:[5,4], borderWidth:1.6, pointRadius:0, fill:false, order:10});
  ds.push({label:'2022–2023 high regime', data:R.term.high, borderColor:C.hi, borderDash:[5,4], borderWidth:1.6, pointRadius:0, fill:{target:'-1', above:'rgba(120,132,144,0.10)'}, order:10});
  ds.push({label:'2005–2024 average', data:R.term.long, borderColor:C.lr, borderDash:[2,3], borderWidth:1.6, pointRadius:0, fill:false, order:9});
  for(const w of ['1M','3M','1Y']) if(termWindowState[w])
    ds.push({label:`Current ${w}`, data:R.term[w], borderColor:winColors[w], backgroundColor:winColors[w],
      borderWidth:2.4, pointRadius:3.2, pointHoverRadius:5, fill:false, order:1});
  return ds;
}
const termChart = new Chart(document.getElementById('termChart'), {
  type:'line', data:{labels:R.term.tenors, datasets:buildTerm()},
  options:{...baseOpts(),
    plugins:{...baseOpts().plugins, tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${fmt(c.parsed.y)} bp`}}},
    scales:{x:{grid:{display:false}, ticks:{font:{size:11.5}}},
      y:{title:{display:true,text:'annualized bp vol',font:{size:11}}, grid:{color:C.line}, beginAtZero:true}}}
});
const twEl = document.getElementById('tsWindows');
['1M','3M','1Y'].forEach(w=>{
  const b=document.createElement('button');
  b.className='chip'+(termWindowState[w]?' on':''); b.textContent='Current '+w;
  b.onclick=()=>{termWindowState[w]=!termWindowState[w]; b.classList.toggle('on');
    termChart.data.datasets=buildTerm(); termChart.update();};
  twEl.appendChild(b);
});

const tenorColors = {'3 Mo':'#8fb3d9','2 Yr':'#3a6ea5','5 Yr':'#6b5b95','10 Yr':'#17293e','30 Yr':'#c75b12'};
let histWin='3M', histRange=10;
const tenorState = {'3 Mo':false,'2 Yr':true,'5 Yr':false,'10 Yr':true,'30 Yr':false};
function buildHist(){
  const [i0,i1]=sliceRange(R.tsDates, histRange);
  const labels=R.tsDates.slice(i0,i1), ds=[];
  const active=R.tsTenors.filter(t=>tenorState[t]);
  for(const t of active)
    ds.push({label:t, data:R.tsSeries[histWin][t].slice(i0,i1), borderColor:tenorColors[t],
      borderWidth:1.8, pointRadius:0, pointHoverRadius:3, fill:false, spanGaps:true, order:1});
  if(active.length===1){
    const t=active[0], n=labels.length;
    const flat=(v,lab,col)=>({label:lab, data:Array(n).fill(v), borderColor:col, borderDash:[5,4],
      borderWidth:1.3, pointRadius:0, pointHoverRadius:0, fill:false, order:10});
    ds.push(flat(R.ref.high[t], `${t} · 2022–2023 high`, C.hi));
    ds.push(flat(R.ref.long[t], `${t} · 2005–2024 avg`, C.lr));
    ds.push(flat(R.ref.low[t],  `${t} · 2012–2021 low`, C.lo));
  }
  return {labels, ds};
}
const h0=buildHist();
const histChart = new Chart(document.getElementById('histChart'), {
  type:'line', data:{labels:h0.labels, datasets:h0.ds},
  options:{...baseOpts(),
    plugins:{...baseOpts().plugins, tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${fmt(c.parsed.y)} bp`}}},
    scales:{x:{grid:{display:false}, ticks:{autoSkip:false, maxRotation:0, font:{size:11}, callback:yearTicks(histRange)}},
      y:{title:{display:true,text:'annualized bp vol',font:{size:11}}, grid:{color:C.line}, beginAtZero:true}}}
});
function refreshHist(){
  const {labels,ds}=buildHist();
  histChart.data.labels=labels; histChart.data.datasets=ds;
  histChart.options.scales.x.ticks.callback=yearTicks(effYears(R.tsDates, histRange));
  histChart.update();
}
const winSeg=document.getElementById('winSeg');
['1M','3M','1Y'].forEach(w=>{
  const b=document.createElement('button'); b.textContent=w; if(w===histWin)b.classList.add('on');
  b.onclick=()=>{histWin=w;[...winSeg.children].forEach(x=>x.classList.remove('on'));b.classList.add('on');refreshHist();};
  winSeg.appendChild(b);
});
addRangeControl(document.getElementById('rangeSeg'), R.tsDates, histRange,
  rg=>{histRange=rg; refreshHist();});
const chipEl=document.getElementById('tenorChips');
R.tsTenors.forEach(t=>{
  const b=document.createElement('button');
  b.className='chip'+(tenorState[t]?' on':'');
  b.innerHTML=`<span class="swatch" style="background:${tenorColors[t]}"></span>${t}`;
  b.onclick=()=>{tenorState[t]=!tenorState[t]; b.classList.toggle('on'); refreshHist();};
  chipEl.appendChild(b);
});

const grid=document.getElementById('grid');
function row(name, vals, cls=''){
  return `<tr class="${cls}"><td>${name}</td>${vals.map(v=>`<td class="mono">${fmt(v)}</td>`).join('')}</tr>`;
}
const T=R.term;
let html=`<thead><tr><th>Annualized bp vol</th>${T.tenors.map(t=>`<th>${t}</th>`).join('')}</tr></thead><tbody>`;
html+=row('Current 1M window',T['1M']);
html+=row('Current 3M window',T['3M']);
html+=row('Current 1Y window',T['1Y'],'rule');
html+=row('2005–2024 average',T.long,'regime');
html+=row('2012–2021 low regime',T.low,'regime');
html+=row('2022–2023 high regime',T.high,'regime rule');
const ratio=T['3M'].map((v,i)=>v!=null&&T.long[i]?v/T.long[i]:null);
html+=`<tr><td>3M &divide; long-run</td>${ratio.map(v=>{
  const cls=v==null?'':(v<0.85?'lo-c':(v>1.15?'hi-c':''));
  return `<td class="mono ${cls}">${v==null?'—':v.toFixed(2)+'×'}</td>`;}).join('')}</tr>`;
if(T.pctile) html+=`<tr><td>3M vol percentile</td>${T.pctile.map(v=>{
  const cls=v==null?'':(v<25?'lo-c':(v>75?'hi-c':''));
  return `<td class="mono ${cls}">${v==null?'—':v.toFixed(0)+'th'}</td>`;}).join('')}</tr>`;
grid.innerHTML=html+'</tbody>';

/* --- yield curve snapshot --- */
const snapStyle=[['now',C.w1y,[],3.2],['m1',C.w3m,[6,4],0],['y1',C.lr,[2,3],0]];
new Chart(document.getElementById('curveChart'),{
  type:'line',
  data:{labels:R.term.tenors, datasets:snapStyle.filter(([k])=>R.curve[k]).map(([k,col,dash,pr])=>({
    label:R.curve[k].date+(k==='now'?' (latest)':k==='m1'?' (1M ago)':' (1Y ago)'),
    data:R.curve[k].vals, borderColor:col, backgroundColor:col, borderDash:dash,
    borderWidth:k==='now'?2.4:1.6, pointRadius:pr, pointHoverRadius:5, fill:false, spanGaps:true}))},
  options:{...baseOpts(),
    plugins:{...baseOpts().plugins, tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${fmt(c.parsed.y,2)}%`}}},
    scales:{x:{grid:{display:false}, ticks:{font:{size:11.5}}},
      y:{title:{display:true,text:'par yield, %',font:{size:11}}, grid:{color:C.line}}}}
});

/* --- yield levels history --- */
let lvlRange=10;
const lvlState={'3 Mo':true,'2 Yr':true,'5 Yr':false,'10 Yr':true,'30 Yr':false};
function buildLvl(){
  const [i0,i1]=sliceRange(R.tsDates, lvlRange);
  const labels=R.tsDates.slice(i0,i1);
  const ds=R.tsTenors.filter(t=>lvlState[t]).map(t=>({label:t, data:R.lvlSeries[t].slice(i0,i1),
    borderColor:tenorColors[t], borderWidth:1.8, pointRadius:0, pointHoverRadius:3, fill:false, spanGaps:true}));
  return {labels, ds};
}
const l0=buildLvl();
const lvlChart=new Chart(document.getElementById('lvlChart'),{
  type:'line', data:{labels:l0.labels, datasets:l0.ds},
  options:{...baseOpts(),
    plugins:{...baseOpts().plugins, tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${fmt(c.parsed.y,2)}%`}}},
    scales:{x:{grid:{display:false}, ticks:{autoSkip:false, maxRotation:0, font:{size:11}, callback:yearTicks(lvlRange)}},
      y:{title:{display:true,text:'par yield, %',font:{size:11}}, grid:{color:C.line}}}}
});
function refreshLvl(){
  const {labels,ds}=buildLvl();
  lvlChart.data.labels=labels; lvlChart.data.datasets=ds;
  lvlChart.options.scales.x.ticks.callback=yearTicks(effYears(R.tsDates, lvlRange));
  lvlChart.update();
}
addRangeControl(document.getElementById('lvlRangeSeg'), R.tsDates, lvlRange,
  rg=>{lvlRange=rg; refreshLvl();});
const lcEl=document.getElementById('lvlChips');
R.tsTenors.forEach(t=>{
  const b=document.createElement('button');
  b.className='chip'+(lvlState[t]?' on':'');
  b.innerHTML=`<span class="swatch" style="background:${tenorColors[t]}"></span>${t}`;
  b.onclick=()=>{lvlState[t]=!lvlState[t]; b.classList.toggle('on'); refreshLvl();};
  lcEl.appendChild(b);
});

/* ================= EQUITIES ================= */

const eqWrap=document.getElementById('eqWrap');
if(!EQ){
  eqWrap.innerHTML=`<section><div class="empty">No equity price data in the cache yet.<br>
    Run <code>python update_dashboard.py</code> with network access to pull MAGS, QQQ, SPY and the Mag&nbsp;7 from Stooq.</div></section>`;
}else{
  const eqColors={MAGS:'#c75b12', QQQ:'#3a6ea5', SPY:'#17293e'};
  eqWrap.innerHTML=`
    <div class="cards" id="eqCards"></div>
    <section>
      <div class="sec-head">
        <h2>Indexed performance &mdash; MAGS vs QQQ vs SPY</h2>
        <div class="controls"><span class="ctl-label">Range</span><div class="seg" id="perfRangeSeg"></div></div>
      </div>
      <p class="sub">Total price return, rebased to 100 at the start of the selected range.</p>
      <div class="chartbox short"><canvas id="perfChart"></canvas></div>
    </section>
    <section>
      <div class="sec-head">
        <h2>Rolling realized vol &mdash; MAGS vs QQQ vs SPY</h2>
        <div class="controls">
          <span class="ctl-label">Window</span><div class="seg" id="eqWinSeg"></div>
          <span class="ctl-label">Range</span><div class="seg" id="eqRangeSeg"></div>
        </div>
      </div>
      <p class="sub">Annualized realized vol (%) of daily log returns. The gap between MAGS and SPY is the concentration premium you pay to hold the Mag&nbsp;7 as a block.</p>
      <div class="chartbox short"><canvas id="eqChart"></canvas></div>
    </section>
    <section>
      <div class="sec-head">
        <h2>Rolling realized vol &mdash; individual Mag 7 stocks</h2>
        <div class="controls">
          <span class="ctl-label">Window</span><div class="seg" id="magWinSeg"></div>
          <span class="ctl-label">Range</span><div class="seg" id="magRangeSeg"></div>
        </div>
      </div>
      <p class="sub">Annualized realized vol (%) for AAPL, MSFT, GOOGL, AMZN, NVDA, META and TSLA, calculated from daily log returns.</p>
      <div class="chartbox"><canvas id="magChart"></canvas></div>
    </section>
    <section>
      <h2>Realized vol grid</h2>
      <p class="sub">Annualized %, by trailing window. Hot column highlights the ratio of each name's 3M vol to SPY's.</p>
      <div style="overflow-x:auto"><table id="eqGrid"></table></div>
    </section>`;

  const eqCards=document.getElementById('eqCards');
  for(const s of EQ.bench){
    const r=EQ.table[s]; if(!r||r['3M']==null) continue;
    eqCards.insertAdjacentHTML('beforeend',`
      <div class="card" style="border-top-color:${eqColors[s]||C.w3m}">
        <div class="t">${s} &middot; 3M window</div>
        <div class="v mono">${r['3M'].toFixed(0)} <span>% ann.</span></div>
        <div class="d">1M ${fmt(r['1M'],0)}% &middot; 1Y ${fmt(r['1Y'],0)}% &middot; YTD ${r.ytd==null?'—':(r.ytd>=0?'+':'')+r.ytd.toFixed(1)+'%'}</div>
      </div>`);
  }
  const spy3=EQ.table.SPY&&EQ.table.SPY['3M'], mags3=EQ.table.MAGS&&EQ.table.MAGS['3M'];
  if(spy3&&mags3){
    const prem=mags3/spy3;
    eqCards.insertAdjacentHTML('beforeend',`
      <div class="card" style="border-top-color:${C.lr}">
        <div class="t">Mag 7 premium &middot; MAGS &divide; SPY</div>
        <div class="v mono">${prem.toFixed(2)}<span>&times;</span></div>
        <div class="d">3M realized vol ratio &mdash; concentration risk vs the broad index</div>
      </div>`);
  }

  let eqWin='3M', eqRange=3;
  function buildEq(){
    const [i0,i1]=sliceRange(EQ.tsDates, eqRange);
    const labels=EQ.tsDates.slice(i0,i1);
    const ds=EQ.bench.map(s=>({label:s, data:EQ.tsSeries[eqWin][s].slice(i0,i1),
      borderColor:eqColors[s]||C.lr, borderWidth:1.8, pointRadius:0, pointHoverRadius:3, fill:false, spanGaps:true}));
    return {labels, ds};
  }
  const e0=buildEq();
  const eqChart=new Chart(document.getElementById('eqChart'),{
    type:'line', data:{labels:e0.labels, datasets:e0.ds},
    options:{...baseOpts(),
      plugins:{...baseOpts().plugins, tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${fmt(c.parsed.y)}%`}}},
      scales:{x:{grid:{display:false}, ticks:{autoSkip:false, maxRotation:0, font:{size:11}, callback:yearTicks(eqRange)}},
        y:{title:{display:true,text:'annualized realized vol, %',font:{size:11}}, grid:{color:C.line}, beginAtZero:true}}}
  });
  function refreshEq(){
    const {labels,ds}=buildEq();
    eqChart.data.labels=labels; eqChart.data.datasets=ds;
    eqChart.options.scales.x.ticks.callback=yearTicks(effYears(EQ.tsDates, eqRange));
    eqChart.update();
  }
  const ews=document.getElementById('eqWinSeg');
  ['1M','3M','1Y'].forEach(w=>{
    const b=document.createElement('button'); b.textContent=w; if(w===eqWin)b.classList.add('on');
    b.onclick=()=>{eqWin=w;[...ews.children].forEach(x=>x.classList.remove('on'));b.classList.add('on');refreshEq();};
    ews.appendChild(b);
  });
  addRangeControl(document.getElementById('eqRangeSeg'), EQ.tsDates, eqRange,
    rg=>{eqRange=rg; refreshEq();});

  /* --- individual Mag 7 realized vol --- */
  const magColors={AAPL:'#577590',MSFT:'#277da1',GOOGL:'#43aa8b',AMZN:'#90be6d',
                   NVDA:'#f9c74f',META:'#f9844a',TSLA:'#d1495b'};
  let magWin='3M', magRange=3;
  function buildMag(){
    const [i0,i1]=sliceRange(EQ.tsDates, magRange);
    const labels=EQ.tsDates.slice(i0,i1);
    const ds=EQ.mag7.map(s=>({label:s, data:EQ.tsSeries[magWin][s].slice(i0,i1),
      borderColor:magColors[s], borderWidth:1.7, pointRadius:0, pointHoverRadius:3,
      fill:false, spanGaps:true}));
    return {labels,ds};
  }
  const m0=buildMag();
  const magChart=new Chart(document.getElementById('magChart'),{
    type:'line', data:{labels:m0.labels,datasets:m0.ds},
    options:{...baseOpts(),
      plugins:{...baseOpts().plugins,tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${fmt(c.parsed.y)}%`}}},
      scales:{x:{grid:{display:false},ticks:{autoSkip:false,maxRotation:0,font:{size:11},callback:yearTicks(magRange)}},
        y:{title:{display:true,text:'annualized realized vol, %',font:{size:11}},grid:{color:C.line},beginAtZero:true}}}
  });
  function refreshMag(){
    const {labels,ds}=buildMag();
    magChart.data.labels=labels; magChart.data.datasets=ds;
    magChart.options.scales.x.ticks.callback=yearTicks(effYears(EQ.tsDates,magRange));
    magChart.update();
  }
  const mws=document.getElementById('magWinSeg');
  ['1M','3M','1Y'].forEach(w=>{
    const b=document.createElement('button'); b.textContent=w; if(w===magWin)b.classList.add('on');
    b.onclick=()=>{magWin=w;[...mws.children].forEach(x=>x.classList.remove('on'));b.classList.add('on');refreshMag();};
    mws.appendChild(b);
  });
  addRangeControl(document.getElementById('magRangeSeg'),EQ.tsDates,magRange,
    rg=>{magRange=rg;refreshMag();});

  /* --- indexed performance --- */
  let perfRange=3;
  function buildPerf(){
    const [i0,i1]=sliceRange(EQ.tsDates, perfRange);
    const labels=EQ.tsDates.slice(i0,i1);
    const ds=EQ.bench.map(s=>{
      const raw=EQ.pxSeries[s].slice(i0,i1);
      const base=raw.find(v=>v!=null);
      return {label:s, data:raw.map(v=>v==null?null:+(v/base*100).toFixed(1)),
        borderColor:eqColors[s]||C.lr, borderWidth:1.8, pointRadius:0, pointHoverRadius:3, fill:false, spanGaps:true};
    });
    return {labels, ds};
  }
  const p0=buildPerf();
  const perfChart=new Chart(document.getElementById('perfChart'),{
    type:'line', data:{labels:p0.labels, datasets:p0.ds},
    options:{...baseOpts(),
      plugins:{...baseOpts().plugins, tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${fmt(c.parsed.y)}`}}},
      scales:{x:{grid:{display:false}, ticks:{autoSkip:false, maxRotation:0, font:{size:11}, callback:yearTicks(perfRange)}},
        y:{title:{display:true,text:'index (start = 100)',font:{size:11}}, grid:{color:C.line}}}}
  });
  addRangeControl(document.getElementById('perfRangeSeg'), EQ.tsDates, perfRange, rg=>{
    perfRange=rg;
    const {labels,ds}=buildPerf();
    perfChart.data.labels=labels; perfChart.data.datasets=ds;
    perfChart.options.scales.x.ticks.callback=yearTicks(effYears(EQ.tsDates, perfRange));
    perfChart.update();
  });

  const eg=document.getElementById('eqGrid');
  const names=[...EQ.bench, ...EQ.mag7];
  let eh=`<thead><tr><th>Ticker</th><th>Last</th><th>YTD</th><th>1M vol</th><th>3M vol</th><th>1Y vol</th><th>3M &divide; SPY</th></tr></thead><tbody>`;
  names.forEach((s,idx)=>{
    const r=EQ.table[s]; if(!r) return;
    const rel=spy3&&r['3M']!=null? r['3M']/spy3 : null;
    const relCls=rel==null?'':(rel>1.5?'hi-c':(rel<1.05?'lo-c':''));
    const cls=(idx===EQ.bench.length-1)?'rule':'';
    eh+=`<tr class="${cls}"><td>${s}</td>
      <td class="mono">${r.last==null?'—':r.last.toFixed(2)}</td>
      <td class="mono">${r.ytd==null?'—':(r.ytd>=0?'+':'')+r.ytd.toFixed(1)+'%'}</td>
      <td class="mono">${fmt(r['1M'])}</td><td class="mono">${fmt(r['3M'])}</td><td class="mono">${fmt(r['1Y'])}</td>
      <td class="mono ${relCls}">${rel==null?'—':rel.toFixed(2)+'×'}</td></tr>`;
  });
  eg.innerHTML=eh+'</tbody>';
}

/* ================= CROSS-ASSET ================= */

const crossWrap=document.getElementById('crossWrap');
if(!XA){
  crossWrap.innerHTML=`<section><div class="empty">Rates-vs-equity comparison needs SPY history &mdash; it will appear after the first online run.</div></section>`;
}else{
  crossWrap.innerHTML=`
    <section>
      <h2>Rates vol vs equity vol &mdash; rolling 3M</h2>
      <p class="sub">10Y Treasury bp vol (left axis) against SPY realized vol in % (right axis). Divergences flag which market is carrying the stress &mdash; a MOVE-vs-VIX read built from realized data.</p>
      <div class="chartbox short"><canvas id="crossChart"></canvas></div>
    </section>`;
  new Chart(document.getElementById('crossChart'),{
    type:'line',
    data:{labels:XA.dates, datasets:[
      {label:'10Y Treasury (bp, left)', data:XA.ust, borderColor:C.w1y, borderWidth:1.8, pointRadius:0, pointHoverRadius:3, fill:false, spanGaps:true, yAxisID:'y'},
      {label:'SPY (%, right)', data:XA.spy, borderColor:C.hi, borderWidth:1.8, pointRadius:0, pointHoverRadius:3, fill:false, spanGaps:true, yAxisID:'y2'},
    ]},
    options:{...baseOpts(),
      plugins:{...baseOpts().plugins, tooltip:{callbacks:{label:c=>` ${c.dataset.label.split(' (')[0]}: ${fmt(c.parsed.y)}${c.dataset.yAxisID==='y'?' bp':'%'}`}}},
      scales:{
        x:{grid:{display:false}, ticks:{autoSkip:false, maxRotation:0, font:{size:11}, callback:yearTicks(0)}},
        y:{position:'left', title:{display:true,text:'10Y bp vol',font:{size:11}}, grid:{color:C.line}, beginAtZero:true},
        y2:{position:'right', title:{display:true,text:'SPY vol, %',font:{size:11}}, grid:{drawOnChartArea:false}, beginAtZero:true},
      }}
  });
}

/* ================= TARIFFS ================= */

new Chart(document.getElementById('tariffChart'),{
  type:'line',
  data:{labels:P.tariffs.years, datasets:[{label:'Average tariff rate',
    data:P.tariffs.rates, borderColor:C.w1y, borderWidth:1.8, pointRadius:0, pointHoverRadius:3,
    fill:{target:'origin', above:'rgba(23,41,62,0.06)'}}]},
  options:{...baseOpts(),
    plugins:{legend:{display:false}, tooltip:{callbacks:{label:c=>` ${c.parsed.y.toFixed(1)}%`}}},
    scales:{
      x:{grid:{display:false}, ticks:{autoSkip:false, maxRotation:0, font:{size:11},
        callback:function(v){const y=+this.getLabelForValue(v); return y%20===0?y:null;}}},
      y:{title:{display:true,text:'% of total imports',font:{size:11}}, grid:{color:C.line}, beginAtZero:true},
    }}
});

/* ================= REGIME ================= */

const RG = P.regime || {};
const regColors = ['#1e7f74', '#8a94a0', '#c75b12', '#a03123'];
const regLabels = ['Low', 'Normal', 'Elevated', 'Stress'];

function banner(el, r, title, unit){
  if(!r){ el.innerHTML=''; return; }
  el.innerHTML = `
    <div class="banner bl-${r.label}">
      <div>
        <div class="lab">${title}</div>
        <div class="reg reg-${r.label}">${r.label}</div>
      </div>
      <div class="kv"><b>${r.pct.toFixed(0)}th</b> percentile of full history<br>
        z-score (log vol) <b>${r.z>=0?'+':''}${r.z.toFixed(2)}</b></div>
      <div class="kv">current 3M vol <b>${r.cur.toFixed(0)} ${unit}</b><br>
        historical median <b>${r.median.toFixed(0)} ${unit}</b></div>
      <div class="kv" style="color:var(--muted)">Low &lt;25th &middot; Normal 25&ndash;75th &middot; Elevated 75&ndash;90th &middot; Stress &gt;90th</div>
    </div>`;
}
banner(document.getElementById('ratesBanner'), RG.rates, 'Current rates vol regime · 2Y/10Y composite', 'bp');
banner(document.getElementById('eqBanner'), RG.eq, 'Current equity vol regime · SPY realized', '%');

/* --- regime timeline strip --- */
(function(){
  const rows=[];
  if(RG.rates) rows.push({name:'Rates', d:RG.rates.stripDates, c:RG.rates.strip});
  if(RG.eq)    rows.push({name:'SPY',   d:RG.eq.stripDates,    c:RG.eq.strip});
  const sec=document.getElementById('stripSec');
  if(!rows.length){ sec.style.display='none'; return; }
  const cv=document.getElementById('stripCanvas');
  function draw(){
    const W=cv.clientWidth, H=110, dpr=window.devicePixelRatio||1;
    cv.width=W*dpr; cv.height=H*dpr;
    const g=cv.getContext('2d'); g.scale(dpr,dpr); g.clearRect(0,0,W,H);
    const left=44, right=6, top=6, rowH=30, gap=12;
    const t0=new Date(rows[0].d[0]).getTime(), t1=new Date(rows[0].d[rows[0].d.length-1]).getTime();
    const X=t=>left+(t-t0)/(t1-t0)*(W-left-right);
    rows.forEach((r,ri)=>{
      const y=top+ri*(rowH+gap);
      g.fillStyle=css('--muted'); g.font='600 10px "Segoe UI",sans-serif';
      g.textBaseline='middle'; g.fillText(r.name, 2, y+rowH/2);
      for(let i=0;i<r.d.length;i++){
        const x=X(new Date(r.d[i]).getTime());
        const x2=i<r.d.length-1?X(new Date(r.d[i+1]).getTime()):W-right;
        g.fillStyle=regColors[r.c[i]];
        g.fillRect(x, y, Math.max(x2-x,1), rowH);
      }
    });
    // year gridlines + labels
    const y0=new Date(t0).getFullYear(), y1=new Date(t1).getFullYear();
    g.font='10px "Segoe UI",sans-serif'; g.textBaseline='top';
    for(let y=y0;y<=y1;y++){
      if(y%5!==0) continue;
      const x=X(new Date(y+'-01-01').getTime());
      if(x<left) continue;
      g.strokeStyle='rgba(255,255,255,.55)'; g.beginPath();
      g.moveTo(x,top); g.lineTo(x,top+rows.length*(rowH+gap)-gap); g.stroke();
      g.fillStyle=css('--muted'); g.fillText(y, x-10, top+rows.length*(rowH+gap)-4);
    }
  }
  draw();
  new ResizeObserver(draw).observe(cv);
})();

/* --- stock–bond correlation --- */
const corrWrap=document.getElementById('corrWrap');
if(RG.corr){
  corrWrap.innerHTML=`
    <section>
      <h2>Stock&ndash;bond return correlation &mdash; rolling 1Y</h2>
      <p class="sub">Correlation of SPY daily returns with 10Y Treasury returns (proxied by &minus;&Delta;yield). Below zero, Treasuries hedge equities; above zero &mdash; as in 2022 &mdash; the 60/40 diversification breaks down.</p>
      <div class="chartbox short"><canvas id="corrChart"></canvas></div>
    </section>`;
  new Chart(document.getElementById('corrChart'),{
    type:'line',
    data:{labels:RG.corr.dates, datasets:[{label:'ρ(SPY, 10Y UST)', data:RG.corr.vals,
      borderColor:C.w1y, borderWidth:1.8, pointRadius:0, pointHoverRadius:3, spanGaps:true,
      fill:{target:{value:0}, above:'rgba(199,91,18,0.10)', below:'rgba(30,127,116,0.10)'}}]},
    options:{...baseOpts(),
      plugins:{legend:{display:false}, tooltip:{callbacks:{label:c=>` ρ = ${c.parsed.y.toFixed(2)}`}}},
      scales:{x:{grid:{display:false}, ticks:{autoSkip:false, maxRotation:0, font:{size:11}, callback:yearTicks(0)}},
        y:{title:{display:true,text:'correlation',font:{size:11}}, grid:{color:C.line},
           min:-1, max:1, ticks:{stepSize:.5}}}}
  });
}

/* ================= TABS ================= */

const tabs=[...document.querySelectorAll('.tab')];
function showTab(name){
  tabs.forEach(t=>{
    const on=t.dataset.tab===name;
    t.classList.toggle('on',on); t.setAttribute('aria-selected',on);
    document.getElementById('panel-'+t.dataset.tab).classList.toggle('on',on);
  });
  history.replaceState(null,'','#'+name);
  // charts created while hidden have zero size — resize them once visible
  requestAnimationFrame(()=>Object.values(Chart.instances).forEach(c=>c.resize()));
}
tabs.forEach(t=>t.onclick=()=>showTab(t.dataset.tab));
const initial=location.hash.slice(1);
if(tabs.some(t=>t.dataset.tab===initial)) showTab(initial);

/* ================= FRESHNESS & REFRESH ================= */

(function(){
  const pill=document.getElementById('freshPill');
  const btn=document.getElementById('refreshBtn');
  const mode=document.getElementById('dateMode');
  const dateInput=document.getElementById('asofDate');
  const applyBtn=document.getElementById('applyDateBtn');
  const ageDays=Math.floor((Date.now()-new Date(R.asOf).getTime())/864e5);
  const stale=ageDays>4;               // >4 calendar days covers weekends + holidays
  pill.textContent = ageDays<=1 ? 'data current' : `rates data ${ageDays} days old`;
  pill.classList.toggle('stale', stale);

  const served = location.protocol.startsWith('http');
  dateInput.max=new Date().toISOString().slice(0,10);
  if(P.selectedAsOf){
    mode.value='date'; dateInput.value=P.selectedAsOf;
    dateInput.style.display=applyBtn.style.display='inline-block';
    pill.textContent=`historical view through ${P.selectedAsOf}`;
  }
  mode.onchange=()=>{
    const historical=mode.value==='date';
    dateInput.style.display=applyBtn.style.display=historical?'inline-block':'none';
    if(!historical && served) doRefresh();
  };
  if(!served){
    if(stale) pill.textContent += ' — run update_dashboard.py';
    return;                             // opened as a plain file: no live refresh possible
  }
  btn.style.display='inline-block';
  applyBtn.onclick=()=>{
    if(!dateInput.value) return;
    applyBtn.disabled=true; applyBtn.textContent='Applying…';
    location.assign(location.pathname+'?asof='+encodeURIComponent(dateInput.value)+location.hash);
  };
  async function doRefresh(){
    btn.disabled=true; btn.textContent='Refreshing… (may take ~1 min)';
    try{
      const r=await fetch('/refresh');
      const j=await r.json();
      if(j.ok){ location.replace(location.pathname+'?r=1'+location.hash); return; }
      btn.textContent='Refresh failed — see server log'; 
      setTimeout(()=>{btn.disabled=false; btn.innerHTML='&#8635; Refresh data';}, 4000);
    }catch(e){
      btn.textContent='Refresh failed — server unreachable';
      setTimeout(()=>{btn.disabled=false; btn.innerHTML='&#8635; Refresh data';}, 4000);
    }
  }
  btn.onclick=doRefresh;
  // auto-refresh on open when stale (skipped right after a refresh via ?r=1)
  if(!P.selectedAsOf && stale && !location.search.includes('r=1')) doRefresh();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
