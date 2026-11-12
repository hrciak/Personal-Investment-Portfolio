"""
Return and risk metrics
=======================

All performance numbers are derived from ONE flow-neutral daily return series,
so deposits and withdrawals never look like gains or crashes:

    r_d = (V_d + W_d) / (V_{d-1} + D_d) - 1

V = portfolio value at day end, D = contributions that day (assumed at the
start of the day, so new money shares the day's move), W = withdrawals that
day (assumed at the end of the day). Chaining (1 + r_d) gives the
time-weighted return (TWR) index; volatility, Sharpe, Sortino, drawdown,
Calmar and beta all use the same r_d. The series is calendar-daily, so it is
annualized with 365 (weekend zeros keep the variance consistent with 252
trading days).

The money-weighted return (XIRR) uses only external flows (deposits,
withdrawals, in-kind transfers at fair value) plus the terminal value —
never trades, which are internal to the portfolio.
"""

import math

import numpy as np
import pandas as pd
from pyxirr import xirr as _xirr

RISK_FREE = 0.025          # annual, used by Sharpe/Sortino
DAYS_PER_YEAR = 365.0
MIN_CAPITAL = 1.0          # EUR; a day starting with less carries no return
MIN_RISK_DAYS = 60         # minimum days with capital before risk stats are shown
MIN_BETA_OBS = 30
DAILY_RETURN_BOUNDS = (-0.6, 1.5)   # outside = data glitch, capped (and reported)


def daily_returns(values, flows_in, flows_out):
    """Flow-neutral daily returns. Returns (array with NaN on days without
    capital, number of days that hit the glitch bounds)."""
    n = len(values)
    r = np.full(n, np.nan)
    capped = 0
    lo, hi = DAILY_RETURN_BOUNDS
    prev = 0.0
    for d in range(n):
        base = prev + flows_in[d]
        if base >= MIN_CAPITAL:
            x = (values[d] + flows_out[d]) / base - 1.0
            if x < lo or x > hi:
                capped += 1
                x = min(max(x, lo), hi)
            r[d] = x
        prev = values[d]
    return r, capped


def twr_index(r) -> np.ndarray:
    """Base-100 TWR index (flat on days without capital)."""
    return 100.0 * np.cumprod(1.0 + np.nan_to_num(r, nan=0.0))


def _span_days(r) -> int:
    """Elapsed calendar days from the first day with capital to the last day —
    the same period definition XIRR and the benchmark CAGR use."""
    idx = np.flatnonzero(~np.isnan(r))
    return int(idx[-1] - idx[0]) if idx.size else 0


def annualized(growth: float, days: int):
    if days <= 0 or growth <= 0:
        return None
    return (growth ** (DAYS_PER_YEAR / days) - 1.0) * 100.0


def performance(r) -> dict:
    """TWR (cumulative %, annualized %) from the daily return series."""
    valid = r[~np.isnan(r)]
    if valid.size == 0:
        return {"twr": None, "twr_annualized": None}
    growth = float(np.prod(1.0 + valid))
    return {"twr": (growth - 1.0) * 100.0,
            "twr_annualized": annualized(growth, _span_days(r))}


def risk_metrics(r) -> dict:
    out = dict.fromkeys(["volatility", "sharpe_ratio", "sortino_ratio", "calmar_ratio",
                         "max_drawdown", "annualized_return"])
    valid = r[~np.isnan(r)]
    if valid.size < MIN_RISK_DAYS:
        return out
    vol = float(np.std(valid, ddof=1)) * math.sqrt(DAYS_PER_YEAR)
    ann = annualized(float(np.prod(1.0 + valid)), _span_days(r))
    downside = math.sqrt(float(np.mean(np.minimum(valid, 0.0) ** 2))) * math.sqrt(DAYS_PER_YEAR)

    idx = np.cumprod(1.0 + valid)
    drawdown = idx / np.maximum.accumulate(idx) - 1.0
    max_dd = float(drawdown.min()) * 100.0

    out["volatility"] = vol * 100.0
    out["annualized_return"] = ann
    if ann is not None:
        rf = RISK_FREE * 100.0
        out["sharpe_ratio"] = (ann - rf) / (vol * 100.0) if vol > 0 else None
        out["sortino_ratio"] = (ann - rf) / (downside * 100.0) if downside > 0 else None
        out["calmar_ratio"] = ann / abs(max_dd) if max_dd < 0 else None
    out["max_drawdown"] = max_dd
    return out


def beta(days: pd.DatetimeIndex, r, bench_obs: pd.Series):
    """Beta vs the benchmark, measured between consecutive benchmark trading
    days on which the portfolio carried capital."""
    if bench_obs is None or bench_obs.empty:
        return None
    capital = ~np.isnan(r)
    if not capital.any():
        return None
    first = days[np.flatnonzero(capital)[0]]
    port = pd.Series(twr_index(r), index=days)
    obs = bench_obs[(bench_obs.index >= first) & (bench_obs.index <= days[-1])]
    obs = obs[obs.index.isin(days)]
    if len(obs) <= MIN_BETA_OBS:
        return None
    rp = port.reindex(obs.index).pct_change().to_numpy()[1:]
    rb = obs.pct_change().to_numpy()[1:]
    mask = ~(np.isnan(rp) | np.isnan(rb))
    rp, rb = rp[mask], rb[mask]
    if rb.size <= MIN_BETA_OBS or np.var(rb) == 0:
        return None
    return float(np.cov(rp, rb, ddof=1)[0, 1] / np.var(rb, ddof=1))


def cagr(prices):
    """Compound annual growth (%) of a daily price array between its first and
    last valid values — a lump-sum, time-weighted benchmark return."""
    idx = np.flatnonzero(~np.isnan(prices))
    if idx.size < 2 or prices[idx[0]] <= 0:
        return None
    return annualized(float(prices[idx[-1]] / prices[idx[0]]), int(idx[-1] - idx[0]))


def mirror_values(prices, flows_in, flows_out) -> np.ndarray:
    """Daily value of a hypothetical portfolio that put every external flow
    (same day, same amount) into the benchmark instead — the "public market
    equivalent". Money arriving before the benchmark has a price waits as cash
    and is invested on its first priced day. NaN until then."""
    n = len(prices)
    out = np.full(n, np.nan)
    units, pending = 0.0, 0.0
    for d in range(n):
        pending += flows_in[d] - flows_out[d]
        p = prices[d]
        if np.isnan(p) or p <= 0:
            continue
        units += pending / p
        pending = 0.0
        out[d] = units * p
    return out


def xirr(flow_days, flows_in, flows_out, end_day, end_value):
    """Money-weighted annual return in %, or None if undefined."""
    dates, amounts = [], []
    for d, fin, fout in zip(flow_days, flows_in, flows_out):
        net = fout - fin            # investor's view: contributions are negative
        if abs(net) > 1e-9:
            dates.append(d)
            amounts.append(net)
    if not any(a < 0 for a in amounts):
        return None
    dates.append(end_day)
    amounts.append(end_value)
    try:
        rate = _xirr(dates, amounts)
    except Exception:  # pyxirr raises when no solution exists
        return None
    if rate is None or not math.isfinite(rate) or abs(rate) > 100:
        return None
    return rate * 100.0
