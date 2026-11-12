"""
Personal Investment Portfolio Dashboard — Flask entry point.

The web layer only serves results: statement loading lives in
parsers.load_statements and all calculations in engine.compute_portfolio.
The computed portfolio is cached in memory; it is rebuilt on the first
request and whenever the dashboard's Reload button POSTs /api/reload
(page views never re-download prices).

Environment variables:
    PIPD_PORT   port to listen on (default 5050)
    PIPD_DEBUG  set to 1 for Flask debug mode + auto-reload (local use only)
"""

import os
import threading
from datetime import datetime

from flask import Flask, jsonify, render_template

from parsers import load_statements
from engine import compute_portfolio, clear_cache

TX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broker-statements")
BROKERS = ("XTB", "Bitpanda", "eToro", "Trading212")

app = Flask(__name__)


class PortfolioCache:
    """Thread-safe holder of the last pipeline run."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = None

    def refresh(self) -> dict:
        with self._lock:
            os.makedirs(TX_DIR, exist_ok=True)
            clear_cache()
            loaded = load_statements(TX_DIR)
            portfolio = compute_portfolio(loaded.transactions)
            self.state = {
                "portfolio": portfolio,
                "load": loaded,
                "warnings": list(loaded.warnings) + list(portfolio.get("warnings", [])),
                "last_updated": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            }
            return self.state

    def get(self) -> dict:
        if self.state is None:
            return self.refresh()
        return self.state


cache = PortfolioCache()


@app.route("/")
def index():
    s = cache.get()
    p, loaded = s["portfolio"], s["load"]
    return render_template(
        "dashboard.html",
        kpis=p["kpis"],
        positions=p["positions"],
        transactions=p["transactions"],
        charts=p["charts"],
        allocation=p["allocation"],
        dividends=p["dividends"],
        risk=p["risk"],
        benchmark=p["benchmark"],
        analytics=p["analytics"],
        source_breakdown=p["source_breakdown"],
        price_report=p["price_report"],
        warnings=s["warnings"],
        last_updated=s["last_updated"],
        sources_loaded=[b for b in BROKERS if loaded.source_counts.get(b, 0) > 0],
        parsed_files=sum(loaded.source_counts.values()),
        parsing_errors=loaded.errors,
    )


@app.route("/api/reload", methods=["POST"])
def reload_data():
    s = cache.refresh()
    return jsonify({
        "status": "success",
        "transactions_count": len(s["load"].transactions),
        "positions_count": len(s["portfolio"]["positions"]),
        "last_updated": s["last_updated"],
        "parsing_errors": s["load"].errors,
        "warnings": s["warnings"],
    })


@app.route("/api/data", methods=["GET"])
def get_data():
    s = cache.get()
    return jsonify({**s["portfolio"], "warnings": s["warnings"], "last_updated": s["last_updated"],
                    "files": s["load"].files, "parsing_errors": s["load"].errors})


if __name__ == "__main__":
    debug = os.environ.get("PIPD_DEBUG") == "1"
    app.run(host="127.0.0.1", port=int(os.environ.get("PIPD_PORT", "5050")), debug=debug)
