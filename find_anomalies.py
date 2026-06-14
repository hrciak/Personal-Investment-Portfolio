#!/usr/bin/env python3
"""
Valuation anomaly scanner
=========================

Finds tickers whose fetched price is implausible relative to what was actually
paid for them — the signature of a wrong-instrument ticker collision (e.g. a
Bitpanda micro-cap token whose symbol also exists as a NYSE stock) or a one-day
data glitch. These produce the giant spikes seen on the performance chart once
closed positions are valued historically.

Reports, per suspicious ticker: source, asset class, max quantity held, average
cost paid, the peak fetched price, the cost multiple, the resulting peak market
value, and the date of the peak — so the bad points can be filtered.

Run with the project venv:  python find_anomalies.py
"""

import os
import sys
import datetime
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import app
from parsers.base import classify_asset
from engine.prices import get_historical_prices

PRICE_COST_MULT = 25.0     # price > this x avg cost  -> suspicious
MIN_PEAK_MV = 300.0        # ignore tiny positions that can't distort the chart


def load():
    txs = app.load_all_transactions()
    seen, dd = set(), []
    for t in txs:
        dtag = t["date"].isoformat() if hasattr(t["date"], "isoformat") else str(t["date"])
        sig = f'{dtag}|{t["ticker"]}|{t["type"]}|{t["qty"]}|{t["price"]}'
        if sig not in seen:
            seen.add(sig)
            dd.append(t)
    return dd


def main():
    dd = load()
    bought_val = defaultdict(float)
    bought_qty = defaultdict(float)
    run_qty = defaultdict(float)
    max_qty = defaultdict(float)
    source = {}
    first_date = None

    for t in sorted(dd, key=lambda x: x["date"]):
        tk = t["ticker"]
        source.setdefault(tk, t["source"])
        if first_date is None:
            first_date = t["date"]
        if t["type"] == "BUY":
            bought_val[tk] += t["qty"] * t["price"] + t["fee"]
            bought_qty[tk] += t["qty"]
            run_qty[tk] += t["qty"]
        elif t["type"] == "SELL":
            run_qty[tk] -= t["qty"]
        max_qty[tk] = max(max_qty[tk], run_qty[tk])

    tickers = [tk for tk in bought_qty if tk != "CASH"]
    start = first_date.date().isoformat() if hasattr(first_date, "date") else str(first_date)[:10]
    today = datetime.date.today().isoformat()
    smap = {tk: source.get(tk, "") for tk in tickers}

    print(f"Fetching price history for {len(tickers)} tickers ...")
    hist = get_historical_prices(tickers, start, today, smap)

    rows = []
    for tk in tickers:
        ps = hist.get(tk)
        if ps is None or len(ps) == 0:
            continue
        avg_cost = bought_val[tk] / bought_qty[tk] if bought_qty[tk] > 0 else 0.0
        pmax = float(ps.max())
        try:
            dmax = str(ps.idxmax().date())
        except Exception:
            dmax = str(ps.idxmax())
        cls = classify_asset(tk, smap.get(tk, ""))
        mult = (pmax / avg_cost) if avg_cost > 0 else float("inf")
        peak_mv = pmax * max_qty[tk]
        if avg_cost > 0 and mult > PRICE_COST_MULT and peak_mv > MIN_PEAK_MV:
            rows.append((peak_mv, tk, source.get(tk), cls, max_qty[tk], avg_cost, pmax, mult, dmax))

    rows.sort(reverse=True)
    print("\n" + "=" * 96)
    print("SUSPICIOUS VALUATIONS  (fetched price >> price actually paid)")
    print("=" * 96)
    if not rows:
        print("None found. No ticker is priced more than "
              f"{PRICE_COST_MULT:.0f}x its average cost.")
        return
    print(f"{'ticker':<8}{'source':<10}{'class':<10}{'maxQty':>15}{'avgCost':>11}"
          f"{'peakPrice':>12}{'xCost':>8}{'peakMV(EUR)':>14}  peakDate")
    print("-" * 96)
    for mv, tk, src, cls, mq, ac, pm, mult, dm in rows:
        mults = f"{mult:>8.0f}" if mult != float("inf") else f"{'inf':>8}"
        print(f"{tk:<8}{str(src):<10}{cls:<10}{mq:>15.4f}{ac:>11.4f}"
              f"{pm:>12.2f}{mults}{mv:>14,.0f}  {dm}")
    print("-" * 96)
    print(f"{len(rows)} ticker(s) flagged. These are almost certainly wrong-instrument")
    print("matches on Yahoo (the symbol collides with an unrelated stock) or one-off data")
    print("glitches. The engine now caps any historical price above "
          f"{PRICE_COST_MULT:.0f}x a position's average")
    print("cost, falling back to cost basis, so they no longer distort the chart.")


if __name__ == "__main__":
    main()
