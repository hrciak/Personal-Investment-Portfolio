#!/usr/bin/env python3
"""
Diagnostics report generator
=============================

Scans broker-statements/, runs every parser, resolves prices for all tickers,
checks the S&P 500 benchmark feed, and writes a timestamped Markdown + JSON
report listing every processing issue so it can be triaged afterwards:

  * Files that could not be parsed (unsupported formats, bad sheets, ...)
  * Tickers whose live price could not be resolved on Yahoo / CoinGecko
  * For each unresolved stock, a probe of common exchange suffixes (.DE, .L,
    .AS, ...) with a suggested working symbol where one is found
  * Whether the benchmark (S&P 500) feed loaded

Usage:
    python diagnostics.py                # full report
    python diagnostics.py --no-probe     # skip the slower exchange-suffix probe

Run it from the project root with the project's virtualenv Python.
"""

import os
import sys
import json
import argparse
from collections import Counter, defaultdict
from datetime import datetime, date

import requests
import yfinance as yf

import app  # reuse the app's loader so we see the exact same parsing behavior
from parsers.base import classify_asset
from engine.prices import get_benchmark_series, COINGECKO_ID_MAP

HERE = os.path.dirname(os.path.abspath(__file__))

# Common non-US exchange suffixes on Yahoo Finance, in rough order of likelihood
# for a European retail portfolio. Used to suggest fixes for tickers that XTB /
# eToro export without an exchange qualifier.
SUFFIX_CANDIDATES = [
    ".DE", ".L", ".AS", ".PA", ".MI", ".SW", ".VI", ".MC",
    ".BR", ".HE", ".ST", ".CO", ".OL", ".LS", ".F", ".IR",
]


def _probe_yahoo(symbol: str) -> float | None:
    """Return a last close price for symbol, or None if Yahoo has no data."""
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist is not None and not hist.empty and "Close" in hist:
            s = hist["Close"].dropna()
            if not s.empty:
                return float(s.iloc[-1])
    except Exception:
        pass
    return None


def _probe_coingecko(ticker: str) -> float | None:
    cg_id = COINGECKO_ID_MAP.get(ticker, ticker.lower())
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=eur",
            timeout=10,
        )
        price = resp.json().get(cg_id, {}).get("eur")
        return float(price) if price is not None else None
    except Exception:
        return None


def build_report(do_probe: bool = True) -> dict:
    # 1. Parse everything through the real app loader
    transactions = app.load_all_transactions()
    parsing_errors = list(app.parsing_errors)
    source_counts = dict(app.source_counts)

    # 2. Collect unique tickers + source map
    source_map = {}
    for t in transactions:
        source_map.setdefault(t["ticker"], t["source"])

    tickers = sorted({t["ticker"] for t in transactions if t["ticker"] != "CASH"})

    stocks_ok, stocks_fail = {}, {}
    crypto_ok, crypto_fail = {}, {}
    suggestions = {}

    for tk in tickers:
        cls = classify_asset(tk, source_map.get(tk, ""))
        if cls == "Crypto":
            price = _probe_coingecko(tk)
            (crypto_ok if price else crypto_fail)[tk] = price
        else:
            price = _probe_yahoo(tk)
            if price:
                stocks_ok[tk] = price
            else:
                stocks_fail[tk] = None
                if do_probe:
                    for suf in SUFFIX_CANDIDATES:
                        alt = tk + suf
                        p = _probe_yahoo(alt)
                        if p:
                            suggestions[tk] = {"symbol": alt, "price": p}
                            break

    # 3. Benchmark check
    today = date.today().isoformat()
    first_date = min((t["date"] for t in transactions), default=None)
    bench_start = first_date.date().isoformat() if first_date else "2020-01-01"
    bench = get_benchmark_series(bench_start, today)
    benchmark_ok = len(bench) > 0

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "totals": {
            "files_with_errors": len(parsing_errors),
            "transactions_parsed": len(transactions),
            "source_counts": source_counts,
            "type_counts": dict(Counter(t["type"] for t in transactions)),
            "unique_tickers": len(tickers),
        },
        "parsing_errors": parsing_errors,
        "price_resolution": {
            "stocks_ok": stocks_ok,
            "stocks_failed": sorted(stocks_fail.keys()),
            "crypto_ok": crypto_ok,
            "crypto_failed": sorted(crypto_fail.keys()),
            "suggested_symbols": suggestions,
        },
        "benchmark": {"ok": benchmark_ok, "points": len(bench), "range": [bench_start, today]},
        "_source_map": source_map,
    }


def render_markdown(rep: dict) -> str:
    t = rep["totals"]
    pr = rep["price_resolution"]
    lines = []
    lines.append("# Portfolio Processing Diagnostics")
    lines.append("")
    lines.append(f"_Generated: {rep['generated_at']}_")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Transactions parsed: **{t['transactions_parsed']}**")
    lines.append(f"- By broker: {', '.join(f'{k}={v}' for k, v in t['source_counts'].items())}")
    lines.append(f"- Unique tickers: **{t['unique_tickers']}**")
    lines.append(f"- Files with errors: **{t['files_with_errors']}**")
    lines.append(f"- Stocks unresolved: **{len(pr['stocks_failed'])}**, "
                 f"Crypto unresolved: **{len(pr['crypto_failed'])}**")
    lines.append(f"- Benchmark (S&P 500): **{'OK' if rep['benchmark']['ok'] else 'FAILED'}** "
                 f"({rep['benchmark']['points']} points)")
    lines.append("")

    # File errors
    lines.append("## 1. File parsing errors")
    lines.append("")
    if rep["parsing_errors"]:
        lines.append("| File | Error |")
        lines.append("|------|-------|")
        for e in rep["parsing_errors"]:
            lines.append(f"| `{e['file']}` | {e['error']} |")
    else:
        lines.append("None. Every file in broker-statements/ parsed cleanly.")
    lines.append("")

    # Unresolved stocks + suggestions
    lines.append("## 2. Unresolved stock tickers")
    lines.append("")
    if pr["stocks_failed"]:
        lines.append("These tickers returned no price from Yahoo. The usual cause is a "
                     "missing exchange suffix (XTB/eToro export e.g. `RHM` for the Frankfurt "
                     "listing `RHM.DE`). Suggested working symbols are probed automatically.")
        lines.append("")
        lines.append("| Ticker | Broker | Suggested Yahoo symbol | Probed price |")
        lines.append("|--------|--------|------------------------|--------------|")
        for tk in pr["stocks_failed"]:
            src = rep["_source_map"].get(tk, "?")
            sug = pr["suggested_symbols"].get(tk)
            if sug:
                lines.append(f"| {tk} | {src} | **{sug['symbol']}** | {sug['price']:.2f} |")
            else:
                lines.append(f"| {tk} | {src} | _none found_ | — |")
    else:
        lines.append("None. All stock tickers resolved.")
    lines.append("")

    # Unresolved crypto
    lines.append("## 3. Unresolved crypto tickers")
    lines.append("")
    if pr["crypto_failed"]:
        lines.append("These crypto symbols are not in the CoinGecko id map "
                     "(engine/prices.py COINGECKO_ID_MAP). Add the mapping to fix.")
        lines.append("")
        for tk in pr["crypto_failed"]:
            lines.append(f"- `{tk}` ({rep['_source_map'].get(tk, '?')})")
    else:
        lines.append("None. All crypto tickers resolved.")
    lines.append("")

    # Benchmark
    lines.append("## 4. Benchmark feed")
    lines.append("")
    b = rep["benchmark"]
    if b["ok"]:
        lines.append(f"Loaded {b['points']} daily points for {b['range'][0]} → {b['range'][1]}.")
    else:
        lines.append("FAILED to load. The S&P 500 line will be empty on the chart. "
                     "Yahoo's `^GSPC` index endpoint commonly times out or rate-limits; "
                     "the engine falls back to SPY/VOO. Re-running usually succeeds.")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("### How to apply ticker fixes")
    lines.append("")
    lines.append("Add confirmed symbols to a ticker override map in `engine/prices.py` "
                 "so the dashboard fetches the right listing (e.g. `RHM` → `RHM.DE`).")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Generate a processing diagnostics report.")
    ap.add_argument("--no-probe", action="store_true",
                    help="Skip the exchange-suffix probe (faster, no fix suggestions).")
    args = ap.parse_args()

    print("Running diagnostics (this fetches live prices, may take a minute)...")
    report = build_report(do_probe=not args.no_probe)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = os.path.join(HERE, f"diagnostics-report-{stamp}.md")
    json_path = os.path.join(HERE, f"diagnostics-report-{stamp}.json")

    md = render_markdown(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    # Drop the internal source map from the JSON payload
    report.pop("_source_map", None)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    pr = report["price_resolution"]
    print("\n" + "=" * 60)
    print(f"  Transactions parsed : {report['totals']['transactions_parsed']}")
    print(f"  File errors         : {report['totals']['files_with_errors']}")
    print(f"  Stocks unresolved   : {len(pr['stocks_failed'])}")
    print(f"  Crypto unresolved   : {len(pr['crypto_failed'])}")
    print(f"  Benchmark           : {'OK' if report['benchmark']['ok'] else 'FAILED'}")
    print("=" * 60)
    print(f"\nReport written to:\n  {md_path}\n  {json_path}")


if __name__ == "__main__":
    main()
