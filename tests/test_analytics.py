from __future__ import annotations

import numpy as np
import pandas as pd

import update_dashboard as dashboard


def _rates(rows: int = 420) -> pd.DataFrame:
    index = pd.bdate_range("2023-01-02", periods=rows)
    base = np.linspace(3.0, 4.0, rows)
    return pd.DataFrame({
        tenor: base + rank * 0.05 + np.sin(np.arange(rows) / (9 + rank)) * 0.1
        for rank, tenor in enumerate(dashboard.TENORS)
    }, index=index)


def _equities(rows: int = 420) -> pd.DataFrame:
    index = pd.bdate_range("2023-01-02", periods=rows)
    time = np.arange(rows)
    return pd.DataFrame({
        ticker: 100 * np.exp(0.0004 * time + 0.01 * np.sin(time / (7 + rank)))
        for rank, ticker in enumerate(dashboard.TICKERS)
    }, index=index)


def test_vol_requires_enough_observations() -> None:
    assert dashboard._vol(pd.Series([1.0, 2.0]), 1.0) is None
    value = dashboard._vol(pd.Series(np.arange(20, dtype=float)), 1.0)
    assert value == round(float(pd.Series(np.arange(20)).std(ddof=1)), 1)


def test_weekly_sampling_keeps_latest_observation() -> None:
    frame = pd.DataFrame({"x": range(12)}, index=pd.bdate_range("2025-01-01", periods=12))
    sampled = dashboard._weekly(frame)
    assert sampled.index[-1] == frame.index[-1]
    assert sampled.iloc[-1, 0] == frame.iloc[-1, 0]


def test_rate_payload_has_expected_units_and_windows() -> None:
    result = dashboard.compute_rates(_rates())
    assert result["term"]["tenors"] == dashboard.TENORS
    assert set(("1M", "3M", "1Y")).issubset(result["term"])
    assert set(result["lastRates"]) == set(dashboard.TENORS)
    assert result["curve"]["now"]["vals"]


def test_equity_and_cross_asset_payloads_are_aligned() -> None:
    rates, equities = _rates(), _equities()
    equity = dashboard.compute_equity(equities)
    cross = dashboard.compute_cross(rates, equities)
    assert equity is not None
    assert equity["bench"] == dashboard.BENCH
    assert cross is not None
    assert len(cross["dates"]) == len(cross["ust"]) == len(cross["spy"])


def test_regime_classification_is_bounded() -> None:
    result = dashboard.compute_regime(_rates(), _equities())
    assert result["rates"]["label"] in {"Low", "Normal", "Elevated", "Stress"}
    assert 0 <= result["rates"]["pct"] <= 100

