import os
import glob
from datetime import datetime
from flask import Flask, render_template, jsonify

from parsers import parse_xtb_xlsx, parse_bitpanda_csv
from engine import compute_portfolio, clear_cache

app = Flask(__name__)

# Server-side caching state
cached_data = None
last_updated = None
source_counts = {"XTB": 0, "Bitpanda": 0}
parsed_files = 0
total_transactions = 0

TX_DIR = os.path.join(os.path.dirname(__file__), "transactions")

def load_all_transactions():
    transactions = []
    global source_counts, parsed_files
    source_counts = {"XTB": 0, "Bitpanda": 0}
    parsed_files = 0
    
    if not os.path.exists(TX_DIR):
        os.makedirs(TX_DIR)
        return transactions

    for filepath in glob.glob(os.path.join(TX_DIR, "*")):
        ext = os.path.splitext(filepath)[1].lower()
        if ext in [".xlsx", ".xls"]:
            try:
                txs = parse_xtb_xlsx(filepath)
                transactions.extend(txs)
                parsed_files += 1
                source_counts["XTB"] += 1
            except Exception as e:
                print(f"Failed to parse XTB XLSX {filepath}: {e}")
        elif ext == ".csv":
            try:
                # Read first 10 lines for signature
                with open(filepath, "r", encoding="utf-8") as f:
                    sample = f.read(2048)
                
                if "Transaction ID" in sample and "Amount Fiat" in sample:
                    txs = parse_bitpanda_csv(filepath)
                    transactions.extend(txs)
                    parsed_files += 1
                    source_counts["Bitpanda"] += 1
                elif "Open time" in sample and "Gross P/L" in sample:
                    print(f"XTB CSV detected ({filepath}); please export as XLSX for best compatibility or implement CSV xtb parser")
                else:
                    print(f"Unknown CSV format: {filepath}")
            except Exception as e:
                print(f"Failed to parse CSV {filepath}: {e}")
                
    return transactions

def trigger_pipeline():
    global cached_data, last_updated, total_transactions
    clear_cache()
    
    txs = load_all_transactions()
    
    # Deduplicate by ID
    # Since ID isn't hashed explicitly in python, we hash (date, ticker, type, qty, price)
    seen = set()
    deduped = []
    for t in txs:
        # Use ISO string for date in hash if possible
        dtag = t["date"].isoformat() if hasattr(t["date"], "isoformat") else str(t["date"])
        sig = f'{dtag}|{t["ticker"]}|{t["type"]}|{t["qty"]}|{t["price"]}'
        if sig not in seen:
            seen.add(sig)
            deduped.append(t)
    
    total_transactions = len(deduped)
    cached_data = compute_portfolio(deduped)
    last_updated = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

@app.route("/")
def index():
    if cached_data is None:
        trigger_pipeline()
    
    sources_loaded = []
    if source_counts["XTB"] > 0: sources_loaded.append("XTB")
    if source_counts["Bitpanda"] > 0: sources_loaded.append("Bitpanda")
    
    return render_template(
        "dashboard.html",
        kpis=cached_data["kpis"],
        positions=cached_data["positions"],
        transactions=cached_data["transactions"],
        charts=cached_data["charts"],
        allocation=cached_data["allocation"],
        dividends=cached_data["dividends"],
        risk=cached_data["risk"],
        last_updated=last_updated,
        sources_loaded=sources_loaded,
        parsed_files=parsed_files
    )

@app.route("/api/reload", methods=["POST"])
def reload_data():
    trigger_pipeline()
    pos_count = len(cached_data["positions"])
    return jsonify({
        "status": "success",
        "transactions_count": total_transactions,
        "positions_count": pos_count,
        "last_updated": last_updated
    })

@app.route("/api/data", methods=["GET"])
def get_data():
    if cached_data is None:
        trigger_pipeline()
    return jsonify(cached_data)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
