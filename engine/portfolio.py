"""
Portfolio engine
================

compute_portfolio(transactions) turns the merged transaction stream from
parsers.load_statements into everything the dashboard and the CLI reports
show. Pipeline:

  1. Ledger replay (engine.ledger), snapshotting holdings, cash and external
     flows at the end of every calendar day.
  2. Price history in EUR for every holding ever owned (engine.prices), each
     feed VALIDATED against the prices actually paid/received in the
     statements: a feed whose median price/trade ratio is far from 1 is a
     different instrument (symbol collision) or the wrong unit, and is
     rejected rather than allowed to spike the chart. Gaps are filled with
     the last observed trade price.
  3. Daily net worth = cash + sum(units x EUR price); flow-neutral returns,
     TWR, risk metrics, beta and XIRR (engine.metrics).

The market-data provider is injectable (`market=`) so the engine can be
tested offline.
"""

import datetime as dt
import math
import statistics

import numpy as np
import pandas as pd

from parsers.base import classify_asset
from engine import metrics
from engine.ledger import Ledger, position_key
from engine.prices import YahooMarketData

POSITION_TYPES = {"BUY", "SELL", "REWARD", "TRANSFER_IN", "TRANSFER_OUT"}
FLOW_LABELS = {"DEPOSIT": "deposit", "WITHDRAWAL": "withdrawal",
               "TRANSFER_IN": "transfer in", "TRANSFER_OUT": "transfer out"}

VALID_RATIO = (0.4, 2.5)       # accepted median feed/trade price ratio
UNIT_RATIO = (0.7, 1.4)        # tighter band for accepting a x100 unit correction
STALE_DAYS = 10                # a feed older than this is not "live"
NEGATIVE_CASH_WARN = -5.0      # EUR


def compute_portfolio(transactions: list, market=None, today: dt.date | None = None) -> dict:
    if not transactions:
        return empty_portfolio()
    market = market or YahooMarketData()
    txs = _prepare(transactions)

    first_day = txs[0]["date"].normalize()
    last_day = max(pd.Timestamp(today or dt.date.today()), txs[-1]["date"].normalize())
    days = pd.date_range(first_day, last_day, freq="D")

    # 1. replay ------------------------------------------------------------
    keys = sorted({position_key(t["source"], t["ticker"]) for t in txs if t["type"] in POSITION_TYPES})
    col = {k: j for j, k in enumerate(keys)}
    n_days, n_keys = len(days), len(keys)
    qty = np.zeros((n_days, n_keys))
    cash = np.zeros(n_days)
    contrib = np.zeros(n_days)
    f_in = np.zeros(n_days)
    f_out = np.zeros(n_days)
    day_flows = [dict() for _ in range(n_days)]
    running_cash = []

    ledger = Ledger()
    i = 0
    for d, day in enumerate(days):
        day_end = day + pd.Timedelta(days=1)
        while i < len(txs) and txs[i]["date"] < day_end:
            tx = txs[i]
            flow = ledger.apply(tx)
            f_in[d] += flow["in"]
            f_out[d] += flow["out"]
            if tx["type"] in FLOW_LABELS:
                day_flows[d][tx["type"]] = day_flows[d].get(tx["type"], 0.0) + tx["qty"] * tx["price"]
            running_cash.append(ledger.cash)
            i += 1
        for k, pos in ledger.positions.items():
            if pos.qty:
                qty[d, col[k]] = pos.qty
        cash[d] = ledger.cash
        contrib[d] = ledger.net_contributions

    # 2. prices ------------------------------------------------------------
    instruments = []
    for k in keys:
        p = ledger.positions[k]
        instruments.append({"key": k, "ticker": p.ticker, "source": p.source, "name": p.name,
                            "asset_class": classify_asset(p.ticker, p.source, p.asset_class_hint),
                            "isin": p.isin, "raw_symbol": p.raw_symbol})
    asset_class = {inst["key"]: inst["asset_class"] for inst in instruments}
    feeds = market.daily_prices_eur(instruments, first_day.date(), last_day.date()) if instruments else {}

    price = np.zeros((n_days, n_keys))
    price_report, price_source = [], {}
    for inst in instruments:
        k = inst["key"]
        pos = ledger.positions[k]
        feed = feeds.get(k)
        status, ratio, note, series = _validate(feed, pos.price_points, asset_class[k])
        price[:, col[k]], price_source[k] = _price_column(series, pos, days, last_day, asset_class[k])
        price_report.append({
            "key": k, "ticker": pos.ticker, "source": pos.source, "asset_class": asset_class[k],
            "symbol": feed.symbol if feed else None, "currency": feed.currency if feed else None,
            "status": status, "validation_ratio": ratio,
            "note": "; ".join([n for n in ([note] + (feed.notes if feed else [])) if n]),
        })

    # 3. valuation & performance --------------------------------------------
    nw = cash + (qty * price).sum(axis=1)
    r, capped = metrics.daily_returns(nw, f_in, f_out)
    twr_idx = metrics.twr_index(r)
    bench_obs, bench_label = market.benchmark_eur(first_day.date(), last_day.date())
    bench_obs = _clean_series(bench_obs)

    total_value = float(nw[-1])
    symbol_of = {k: (f.symbol if f else None) for k, f in feeds.items()}
    positions = _open_positions(ledger, keys, col, qty, price, price_source, asset_class, symbol_of,
                                ledger.cash)
    market_value = sum(p["market_value"] for p in positions)
    unrealized = sum(p["unrealized_pnl"] for p in positions)
    realized = ledger.realized
    total_pnl = realized + unrealized + ledger.income_net - ledger.other_fees
    perf = metrics.performance(r)

    kpis = {
        "total_value": total_value,
        "cash_balance": ledger.cash,
        "market_value": market_value,
        "net_contributions": ledger.net_contributions,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl / ledger.net_contributions * 100 if ledger.net_contributions > 0 else None,
        "xirr": metrics.xirr([d.date() for d in days], f_in, f_out, last_day.date(), total_value),
        "twr": perf["twr"],
        "twr_annualized": perf["twr_annualized"],
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "dividend_income": ledger.dividends,
        "interest_income": ledger.interest,
        "reward_income": ledger.rewards,
        "withholding_tax": ledger.withholding_tax,
        "income_net": ledger.income_net,
        "total_fees": ledger.total_fees,
        "total_invested": ledger.total_invested,
        "unreconciled": (total_value - ledger.net_contributions) - total_pnl,
    }
    risk = metrics.risk_metrics(r)
    risk["beta"] = metrics.beta(days, r, bench_obs)

    bench_daily = _benchmark_daily(bench_obs, days)
    mirror = metrics.mirror_values(bench_daily, f_in, f_out)
    benchmark = _benchmark_comparison(bench_label, bench_daily, mirror, days, f_in, f_out,
                                      kpis, last_day)

    result = {
        "kpis": kpis,
        "risk": risk,
        "benchmark": benchmark,
        "analytics": _analytics(ledger, positions),
        "allocation": _allocation(positions, ledger.cash),
        "source_breakdown": _source_breakdown(ledger, positions),
        "positions": positions,
        "transactions": _transactions_out(txs, running_cash, asset_class),
        "dividends": _dividends_out(ledger, positions),
        "charts": {
            "dates": [d.strftime("%Y-%m-%d") for d in days],
            "net_worth": nw.tolist(),
            "invested": contrib.tolist(),
            "benchmark": _benchmark_index(bench_daily),
            "benchmark_value": [None if np.isnan(v) else round(float(v), 2) for v in mirror],
            "events": [float(nw[d]) if day_flows[d] else None for d in range(n_days)],
            "event_labels": [_flow_label(day_flows[d]) for d in range(n_days)],
            "twr": twr_idx.tolist(),
        },
        "price_report": sorted(price_report, key=lambda x: (x["status"] == "ok", x["key"])),
        "warnings": _warnings(ledger, positions, price_report, capped),
        "meta": {"first_date": first_day.strftime("%Y-%m-%d"), "last_date": last_day.strftime("%Y-%m-%d"),
                 "benchmark": bench_label, "capped_return_days": capped},
    }
    return _sanitize(result)


# --------------------------------------------------------------------------
# Preparation & pricing
# --------------------------------------------------------------------------

def _prepare(transactions: list) -> list:
    txs = []
    for t in transactions:
        ts = pd.Timestamp(t["date"])
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        txs.append({**t, "date": ts, "fee": float(t.get("fee") or 0.0),
                    "qty": float(t.get("qty") or 0.0), "price": float(t.get("price") or 0.0)})
    txs.sort(key=lambda x: x["date"])  # stable: keeps the loader's order within a timestamp
    return txs


def _clean_series(s):
    if s is None or len(s) == 0:
        return None
    s = s.dropna()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s = pd.Series(s.to_numpy(dtype=float), index=idx.normalize())
    return s[~s.index.duplicated(keep="last")].sort_index()


def _validate(feed, points, asset_class):
    """Return (status, median ratio, note, usable EUR series or None)."""
    if asset_class == "Cash":
        return "cash", None, "", None
    series = _clean_series(feed.series) if feed is not None else None
    if series is None or series.empty:
        return "missing", None, "no price feed — valued at last trade price", None

    idx = series.index
    vals = series.to_numpy()
    ratios = []
    for date, px in points:
        if px <= 0:
            continue
        day = pd.Timestamp(date).normalize()
        j = idx.searchsorted(day, side="right") - 1
        if j >= 0 and (day - idx[j]).days <= 7 and vals[j] > 0:
            ratios.append(vals[j] / px)

    lo, hi = VALID_RATIO
    if not ratios:
        # No trade overlaps the feed. Feeds from an explicit id map are trusted;
        # ticker-guessed symbols get a coarse collision guard.
        trusted = str(feed.symbol or "").startswith("coingecko:")
        ref = max((px for _, px in points if px > 0), default=None)
        if not trusted and ref and float(series.max()) > 25 * ref:
            return "rejected", None, "feed price >25x any traded price — likely a different instrument", None
        return "ok", None, "not verifiable against trade prices", series
    m = statistics.median(ratios)
    if lo <= m <= hi:
        return "ok", m, "", series
    # Listings quoted in pence/cents when the currency lookup said pounds (or
    # vice versa): accept only a near-exact x100 mismatch, and never for crypto,
    # where a 100x-off price is a different coin, not a unit problem.
    if asset_class != "Crypto":
        for scale, label in ((0.01, "quoted in minor units (x0.01)"), (100.0, "quoted in major units (x100)")):
            if UNIT_RATIO[0] <= m * scale <= UNIT_RATIO[1]:
                return "ok", m * scale, label, series * scale
    split = _split_hint(m) if asset_class != "Crypto" else None
    if split:
        return "rejected", m, (f"feed/trade price ratio {m:.3g} — consistent with a {split} stock split "
                               "after these trades; valued at the traded prices"), None
    return "rejected", m, (f"feed/trade price ratio {m:.3g} — wrong instrument or unit; "
                           "valued at last trade price (fix via symbol_overrides.json)"), None


def _split_hint(ratio: float):
    """'4:1' if a ratio looks like a split adjustment applied after the trades."""
    for n in (2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50):
        if abs(ratio * n - 1) < 0.15:
            return f"{n}:1"
        if abs(ratio / n - 1) < 0.15:
            return f"1:{n} reverse"
    return None


def _price_column(series, pos, days, last_day, asset_class):
    """Daily EUR price for one holding: validated feed, else last trade price."""
    if asset_class == "Cash":
        return np.ones(len(days)), "live"
    if pos.price_points:
        pts = pd.Series([p for _, p in pos.price_points],
                        index=[pd.Timestamp(d).normalize() for d, _ in pos.price_points])
        pts = pts[pts > 0]
        fallback = pts.groupby(level=0).last().reindex(days).ffill() if not pts.empty else None
    else:
        fallback = None

    source = "cost"
    if series is not None:
        feed = series.reindex(series.index.union(days)).ffill().reindex(days)
        col = feed.to_numpy(dtype=float)
        if (last_day - series.index[-1]).days <= STALE_DAYS:
            source = "live"
    else:
        col = np.full(len(days), np.nan)
    if fallback is not None:
        col = np.where(np.isnan(col), fallback.to_numpy(dtype=float), col)
    if source != "live" and pos.export_price:
        col[-1] = pos.export_price          # broker's own mark at export
        source = "export"
    return np.nan_to_num(col, nan=0.0), source


# --------------------------------------------------------------------------
# Output sections
# --------------------------------------------------------------------------

def _open_positions(ledger, keys, col, qty, price, price_source, asset_class, symbol_of, cash):
    rows = []
    for k in keys:
        pos = ledger.positions[k]
        if not pos.is_open:
            continue
        units = float(qty[-1, col[k]])
        px = float(price[-1, col[k]])
        mv = units * px
        unreal = mv - pos.cost
        rows.append({
            "key": k, "ticker": pos.ticker, "name": pos.name, "asset_class": asset_class[k],
            "source": pos.source, "symbol": symbol_of.get(k), "qty": units, "avg_cost": pos.avg_cost,
            "price": px, "price_source": price_source[k], "market_value": mv,
            "unrealized_pnl": unreal,
            "unrealized_pnl_pct": unreal / pos.cost * 100 if pos.cost > 0 else None,
            "realized_pnl": pos.realized, "weight_pct": 0.0,
        })
    gross = sum(r["market_value"] for r in rows) + max(cash, 0.0)
    for r in rows:
        r["weight_pct"] = r["market_value"] / gross * 100 if gross > 0 else 0.0
    rows.sort(key=lambda x: x["market_value"], reverse=True)
    return rows


def _analytics(ledger, positions):
    wins = [x for x in ledger.trade_results if x > 0]
    losses = [x for x in ledger.trade_results if x < 0]
    closed = len(wins) + len(losses)
    mv_total = sum(p["market_value"] for p in positions)
    hhi = (sum((p["market_value"] / mv_total) ** 2 for p in positions) * 10000) if mv_total > 0 else 0.0
    inv = ledger.total_invested
    return {
        "win_rate": len(wins) / closed * 100 if closed else None,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "hhi": hhi,
        "cost_drag_pct": (ledger.total_fees + ledger.withholding_tax) / inv * 100 if inv > 0 else 0.0,
        "div_yield_on_cost": ledger.dividends / inv * 100 if inv > 0 else 0.0,
        "positions_count": len(positions),
        "closed_count": closed,
    }


def _allocation(positions, cash):
    alloc = {"Stock/ETF": 0.0, "Crypto": 0.0, "Cash": max(cash, 0.0)}
    for p in positions:
        alloc[p["asset_class"]] = alloc.get(p["asset_class"], 0.0) + p["market_value"]
    return alloc


def _source_breakdown(ledger, positions):
    out = []
    for name in sorted(ledger.by_source):
        s = ledger.by_source[name]
        held = [p for p in positions if p["source"] == name]
        out.append({
            "name": name, "invested": s.invested, "realized_pnl": s.realized_pnl, "fees": s.fees,
            "dividends": s.dividends, "market_value": sum(p["market_value"] for p in held),
            "unrealized_pnl": sum(p["unrealized_pnl"] for p in held), "positions_count": len(held),
            "cash_balance": s.cash, "net_contributions": s.net_contributions,
        })
    return out


def _transactions_out(txs, running_cash, asset_class):
    out = []
    for tx, rc in zip(txs, running_cash):
        k = position_key(tx["source"], tx["ticker"])
        cls = asset_class.get(k) or classify_asset(tx["ticker"], tx["source"], tx.get("asset_class_hint", ""))
        out.append({
            "date": tx["date"].strftime("%d.%m.%Y %H:%M"),
            "date_iso": tx["date"].strftime("%Y-%m-%dT%H:%M:%S"),
            "source": tx["source"], "ticker": tx["ticker"], "type": tx["type"],
            "qty": tx["qty"], "price": tx["price"], "total": tx["qty"] * tx["price"],
            "fee": tx["fee"], "running_cash": rc, "asset_class": cls,
        })
    out.reverse()
    return out


def _dividends_out(ledger, positions):
    cost_by_ticker = {}
    for p in positions:
        pos = ledger.positions[p["key"]]
        cost_by_ticker[p["ticker"]] = cost_by_ticker.get(p["ticker"], 0.0) + pos.cost
    rows = []
    for ticker, d in ledger.dividends_by_ticker.items():
        cost = cost_by_ticker.get(ticker, 0.0)
        rows.append({"ticker": ticker, "total_eur": d["total_eur"], "count": d["count"],
                     "last_date": pd.Timestamp(d["last_date"]).strftime("%d.%m.%Y"),
                     "yield_pct": d["total_eur"] / cost * 100 if cost > 0 else 0.0})
    rows.sort(key=lambda x: x["total_eur"], reverse=True)
    return rows


def _benchmark_daily(bench_obs, days) -> np.ndarray:
    """Benchmark EUR level on every calendar day (forward-filled), NaN before data."""
    if bench_obs is None:
        return np.full(len(days), np.nan)
    daily = bench_obs.reindex(bench_obs.index.union(days)).ffill().reindex(days)
    return daily.to_numpy(dtype=float)


def _benchmark_index(daily: np.ndarray) -> list:
    valid = daily[~np.isnan(daily)]
    if valid.size == 0 or valid[0] <= 0:
        return [None] * len(daily)
    return [None if np.isnan(v) else round(float(v / valid[0] * 100.0), 6) for v in daily]


def _benchmark_comparison(label, daily, mirror, days, f_in, f_out, kpis, last_day) -> dict:
    """Two like-for-like comparisons with the benchmark:
      * money-weighted: your XIRR vs the XIRR of the same cash flows invested in
        the benchmark (public market equivalent), plus the EUR difference;
      * time-weighted: your annualized TWR vs the benchmark's CAGR."""
    mirror_value = None if np.isnan(mirror[-1]) else float(mirror[-1])
    mirror_xirr = (metrics.xirr([d.date() for d in days], f_in, f_out, last_day.date(), mirror_value)
                   if mirror_value is not None else None)
    bench_cagr = metrics.cagr(daily)

    def diff(a, b):
        return a - b if a is not None and b is not None else None

    return {
        "label": label,
        "start": days[0].strftime("%d.%m.%Y"),
        "end": days[-1].strftime("%d.%m.%Y"),
        "years": (days[-1] - days[0]).days / 365.0,
        "mirror_value": mirror_value,
        "mirror_xirr": mirror_xirr,
        "value_difference": diff(kpis["total_value"], mirror_value),
        "xirr_difference": diff(kpis["xirr"], mirror_xirr),
        "cagr": bench_cagr,
        "twr_difference": diff(kpis["twr_annualized"], bench_cagr),
    }


def _flow_label(flows: dict):
    if not flows:
        return None
    parts = []
    for t in ("DEPOSIT", "TRANSFER_IN", "WITHDRAWAL", "TRANSFER_OUT"):
        if t in flows:
            sign = "+" if t in ("DEPOSIT", "TRANSFER_IN") else "−"
            parts.append(f"{sign}€{flows[t]:,.0f} {FLOW_LABELS[t]}")
    return " · ".join(parts)


def _warnings(ledger, positions, price_report, capped):
    notes = []
    for name, s in sorted(ledger.by_source.items()):
        if s.cash < NEGATIVE_CASH_WARN:
            notes.append(f"{name}: reconstructed cash balance is −€{-s.cash:,.2f}. Some deposits "
                         "(or sales) are probably missing from the statements, so returns for this "
                         "broker are overstated.")
    for key, units in sorted(ledger.oversold.items()):
        notes.append(f"{key}: sold {units:,.6g} more units than the statements show were bought — "
                     "earlier purchases are probably missing.")
    open_keys = {p["key"] for p in positions}
    bad = [p for p in price_report if p["key"] in open_keys and p["status"] in ("rejected", "missing")]
    if bad:
        names = ", ".join(f"{p['ticker']} ({p['source']})" for p in bad)
        notes.append(f"No usable live price for {len(bad)} holding(s): {names}. They are valued at "
                     "their last trade/statement price — see Price sources.")
    if capped:
        notes.append(f"{capped} day(s) had an implausible daily return (bad price data) and were "
                     "capped in the TWR/risk calculations.")
    return [{"file": None, "message": m} for m in notes]


def _sanitize(obj):
    """Make the result JSON-safe: numpy scalars -> Python, NaN/inf -> None."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def empty_portfolio() -> dict:
    return {
        "kpis": {k: 0.0 for k in ("total_value", "cash_balance", "market_value", "net_contributions",
                                  "total_pnl", "realized_pnl", "unrealized_pnl", "dividend_income",
                                  "interest_income", "reward_income", "withholding_tax", "income_net",
                                  "total_fees", "total_invested", "unreconciled")}
                | {"total_pnl_pct": None, "xirr": None, "twr": None, "twr_annualized": None},
        "risk": dict.fromkeys(["volatility", "sharpe_ratio", "sortino_ratio", "calmar_ratio",
                               "max_drawdown", "annualized_return", "beta"]),
        "benchmark": dict.fromkeys(["label", "start", "end", "years", "mirror_value", "mirror_xirr",
                                    "value_difference", "xirr_difference", "cagr", "twr_difference"]),
        "analytics": {"win_rate": None, "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": None,
                      "hhi": 0.0, "cost_drag_pct": 0.0, "div_yield_on_cost": 0.0,
                      "positions_count": 0, "closed_count": 0},
        "allocation": {"Stock/ETF": 0.0, "Crypto": 0.0, "Cash": 0.0},
        "source_breakdown": [],
        "positions": [],
        "transactions": [],
        "dividends": [],
        "charts": {"dates": [], "net_worth": [], "invested": [], "benchmark": [], "benchmark_value": [],
                   "events": [], "event_labels": [], "twr": []},
        "price_report": [],
        "warnings": [],
        "meta": {"first_date": None, "last_date": None, "benchmark": None, "capped_return_days": 0},
    }
