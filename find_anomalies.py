#!/usr/bin/env python3
"""
Valuation anomaly scanner
=========================

Shows how the engine judged every price feed. Each feed is validated against
the prices actually paid/received in the statements (median feed/trade price
ratio): a ratio far from 1 is the signature of a wrong-instrument symbol
collision (e.g. a Bitpanda micro-cap token whose symbol is also a NYSE stock)
or a wrong unit (pence vs pounds). Rejected feeds are not used — the holding
is valued at its last trade price instead — so they cannot spike the chart.

Listed per holding: status, validation ratio, symbol, quote currency, note.
Also reports days whose daily return was capped as a data glitch.

Run with the project venv:  python find_anomalies.py
"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from parsers import load_statements
from engine import compute_portfolio

TX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broker-statements")


def main():
    loaded = load_statements(TX_DIR)
    if not loaded.transactions:
        print("No transactions found.")
        return
    print("Fetching price history and validating feeds ...")
    portfolio = compute_portfolio(loaded.transactions)
    open_keys = {p["key"] for p in portfolio["positions"]}
    rows = portfolio["price_report"]

    flagged = [r for r in rows if r["status"] in ("rejected", "missing")
               or (r["validation_ratio"] is not None and not 0.8 <= r["validation_ratio"] <= 1.25)]
    print("\n" + "=" * 100)
    print("PRICE FEED VALIDATION  (ratio = feed price / price actually traded, median)")
    print("=" * 100)
    print(f"{'holding':<24}{'open':<6}{'status':<10}{'ratio':>8}  {'symbol':<18}{'ccy':<6}note")
    print("-" * 100)
    for r in sorted(flagged, key=lambda x: (x["status"], x["key"])):
        ratio = f"{r['validation_ratio']:.3g}" if r["validation_ratio"] is not None else "—"
        print(f"{r['key']:<24}{'yes' if r['key'] in open_keys else 'no':<6}{r['status']:<10}"
              f"{ratio:>8}  {str(r['symbol']):<18}{str(r['currency']):<6}{r['note']}")
    if not flagged:
        print("No anomalies: every feed matches the traded prices.")
    print("-" * 100)
    ok = sum(r["status"] == "ok" for r in rows)
    print(f"{ok}/{len(rows)} feeds accepted; {len(flagged)} flagged "
          "(rejected/missing feeds fall back to the last trade price).")
    capped = portfolio["meta"].get("capped_return_days", 0)
    if capped:
        print(f"{capped} day(s) had an implausible daily portfolio return and were capped.")
    print("Fix a wrong symbol with symbol_overrides.json (see engine/symbols.py).")


if __name__ == "__main__":
    main()
