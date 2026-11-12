#!/usr/bin/env python3
"""
Diagnostics report generator
============================

Runs the exact pipeline the dashboard uses (parsers.load_statements +
engine.compute_portfolio) and writes a timestamped Markdown + JSON report
(gitignored) listing every processing issue:

  * files that could not be parsed
  * data-quality notes (statement coverage gaps, positions closed in an
    uncovered period, negative reconstructed cash, unmatched sales, ...)
  * holdings whose price feed is missing or was rejected by the engine's
    trade-price validation, with the symbol and currency that were tried
  * for unresolved stocks, an optional probe of common exchange suffixes
    (.DE, .L, .AS, ...) suggesting a symbol for symbol_overrides.json
  * whether the S&P 500 benchmark loaded

Usage:
    python diagnostics.py              # full report
    python diagnostics.py --no-probe   # skip the slower exchange-suffix probe
"""

import os
import sys
import json
import argparse
from collections import Counter
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import yfinance as yf

from parsers import load_statements
from engine import compute_portfolio
from engine.symbols import OVERRIDES_PATH

HERE = os.path.dirname(os.path.abspath(__file__))
TX_DIR = os.path.join(HERE, "broker-statements")

# Common Yahoo exchange suffixes for a European retail portfolio
SUFFIX_CANDIDATES = [".DE", ".L", ".AS", ".PA", ".MI", ".SW", ".VI", ".MC",
                     ".BR", ".HE", ".ST", ".CO", ".OL", ".LS", ".F", ".IR"]


def _probe_yahoo(symbol: str):
    """(last close, currency) for a Yahoo symbol, or None if it has no data."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="5d")
        if hist is not None and not hist.empty:
            close = hist["Close"].dropna()
            if not close.empty:
                return float(close.iloc[-1]), t.fast_info["currency"]
    except Exception:  # yfinance raises many error types for unknown symbols
        pass
    return None


def build_report(do_probe: bool = True) -> dict:
    loaded = load_statements(TX_DIR)
    portfolio = compute_portfolio(loaded.transactions)
    open_keys = {p["key"] for p in portfolio["positions"]}

    problems = [dict(p, open=p["key"] in open_keys) for p in portfolio["price_report"]
                if p["status"] in ("missing", "rejected")]
    suggestions = {}
    if do_probe:
        for p in problems:
            # A split-adjusted feed is the right instrument — another listing won't help.
            if p["asset_class"] != "Stock/ETF" or p["ticker"] in suggestions or "split" in p["note"]:
                continue
            base = p["ticker"].replace(".", "-")
            for suf in SUFFIX_CANDIDATES:
                hit = _probe_yahoo(base + suf)
                if hit:
                    suggestions[p["ticker"]] = {"symbol": base + suf, "price": hit[0], "currency": hit[1]}
                    break

    bench_ok = any(v is not None for v in portfolio["charts"]["benchmark"])
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "totals": {
            "files": len(loaded.files),
            "files_with_errors": len(loaded.errors),
            "transactions": len(loaded.transactions),
            "source_counts": dict(loaded.source_counts),
            "type_counts": dict(Counter(t["type"] for t in loaded.transactions)),
            "holdings_ever": len(portfolio["price_report"]),
            "holdings_open": len(open_keys),
        },
        "files": loaded.files,
        "parsing_errors": loaded.errors,
        "warnings": list(loaded.warnings) + list(portfolio["warnings"]),
        "price_problems": problems,
        "price_report": portfolio["price_report"],
        "suggested_symbols": suggestions,
        "benchmark": {"ok": bench_ok, "label": portfolio["meta"].get("benchmark")},
    }


def render_markdown(rep: dict) -> str:
    t = rep["totals"]
    out = ["# Portfolio Processing Diagnostics", "", f"_Generated: {rep['generated_at']}_", "",
           "## Summary", "",
           f"- Files: **{t['files']}** parsed, **{t['files_with_errors']}** with errors",
           f"- Transactions after merging: **{t['transactions']}**",
           f"- By broker (files): {', '.join(f'{k}={v}' for k, v in t['source_counts'].items())}",
           f"- Holdings: **{t['holdings_open']}** open of {t['holdings_ever']} ever held",
           f"- Price problems: **{len(rep['price_problems'])}** "
           f"({sum(p['open'] for p in rep['price_problems'])} affect open holdings)",
           f"- Benchmark: **{'OK' if rep['benchmark']['ok'] else 'FAILED'}** ({rep['benchmark']['label']})",
           ""]

    out += ["## 1. File parsing errors", ""]
    if rep["parsing_errors"]:
        out += ["| File | Error |", "|------|-------|"]
        out += [f"| `{e['file']}` | {e['error']} |" for e in rep["parsing_errors"]]
    else:
        out.append("None. Every file in broker-statements/ parsed cleanly.")
    out.append("")

    out += ["## 2. Data-quality notes", ""]
    if rep["warnings"]:
        out += [f"- {('`' + w['file'] + '`: ') if w.get('file') else ''}{w['message']}" for w in rep["warnings"]]
    else:
        out.append("None.")
    out.append("")

    out += ["## 3. Holdings without a usable price feed", ""]
    if rep["price_problems"]:
        out += ["These holdings are valued at their last trade/statement price instead of a live quote.",
                "", "| Holding | Open | Class | Symbol tried | Status | Note | Suggested symbol |",
                "|---------|------|-------|--------------|--------|------|------------------|"]
        for p in rep["price_problems"]:
            sug = rep["suggested_symbols"].get(p["ticker"])
            sug_s = f"**{sug['symbol']}** ({sug['price']:.2f} {sug['currency']})" if sug else "—"
            out.append(f"| {p['ticker']} ({p['source']}) | {'yes' if p['open'] else 'no'} | "
                       f"{p['asset_class']} | `{p['symbol']}` | {p['status']} | {p['note']} | {sug_s} |")
    else:
        out.append("None. Every holding has a validated price feed.")
    out.append("")

    out += ["## 4. Benchmark feed", ""]
    out.append(f"Loaded: {rep['benchmark']['label']}." if rep["benchmark"]["ok"] else
               "FAILED to load — Yahoo commonly rate-limits; the engine tries ^SP500TR, SPY and "
               "^GSPC in turn. Re-running usually succeeds.")
    out += ["", "---", "", "### How to fix a wrong or missing symbol", "",
            f"Create `{os.path.basename(OVERRIDES_PATH)}` in the project root, e.g. "
            "`{\"RHM\": \"RHM.DE\", \"Bitpanda:BEST\": null}` (null disables the feed). "
            "Keys are `TICKER` or `Broker:TICKER`. Then press Reload in the dashboard.", ""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Generate a processing diagnostics report.")
    ap.add_argument("--no-probe", action="store_true",
                    help="Skip the exchange-suffix probe (faster, no fix suggestions).")
    args = ap.parse_args()

    print("Running diagnostics (fetches live prices, may take a minute)...")
    report = build_report(do_probe=not args.no_probe)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = os.path.join(HERE, f"diagnostics-report-{stamp}.md")
    json_path = os.path.join(HERE, f"diagnostics-report-{stamp}.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    t = report["totals"]
    print("\n" + "=" * 60)
    print(f"  Transactions        : {t['transactions']}")
    print(f"  File errors         : {t['files_with_errors']}")
    print(f"  Data-quality notes  : {len(report['warnings'])}")
    print(f"  Price problems      : {len(report['price_problems'])}")
    print(f"  Benchmark           : {'OK' if report['benchmark']['ok'] else 'FAILED'}")
    print("=" * 60)
    print(f"\nReport written to:\n  {md_path}\n  {json_path}")


if __name__ == "__main__":
    main()
