import os
from datetime import datetime
from flask import Flask, render_template, jsonify

from parsers import (parse_xtb_xlsx, parse_bitpanda_csv, parse_etoro, is_etoro_file,
                     parse_trading212_csv, is_trading212_file)
from engine import compute_portfolio, clear_cache

app = Flask(__name__)

# Server-side caching state
cached_data = None
last_updated = None
source_counts = {"XTB": 0, "Bitpanda": 0, "eToro": 0, "Trading212": 0}
parsed_files = 0
total_transactions = 0
parsing_errors = []

TX_DIR = os.path.join(os.path.dirname(__file__), "broker-statements")

def load_all_transactions():
    transactions = []
    global source_counts, parsed_files, parsing_errors
    source_counts = {"XTB": 0, "Bitpanda": 0, "eToro": 0, "Trading212": 0}
    parsed_files = 0
    parsing_errors = []
    
    if not os.path.exists(TX_DIR):
        os.makedirs(TX_DIR)
        return transactions

    # os.listdir instead of glob: glob treats [ ] in the directory path as
    # pattern syntax and silently matches nothing
    for filename in sorted(os.listdir(TX_DIR)):
        filepath = os.path.join(TX_DIR, filename)
        if not os.path.isfile(filepath):
            continue
        # Skip hidden files
        if os.path.basename(filepath).startswith("."):
            continue
            
        ext = os.path.splitext(filepath)[1].lower()
        if ext in [".xlsx", ".xls"]:
            # Detect eToro XLSX/XLS first (by sheet names)
            is_etoro = False
            try:
                is_etoro = is_etoro_file(filepath)
            except Exception:
                pass
                
            if is_etoro:
                try:
                    txs = parse_etoro(filepath)
                    transactions.extend(txs)
                    parsed_files += 1
                    source_counts["eToro"] += 1
                except Exception as e:
                    err_msg = str(e)
                    if "does not support the old .xls" in err_msg or ext == ".xls":
                        err_msg = "Excel 97-2003 (.xls) format is not supported by openpyxl. Please convert the file to .xlsx or .csv."
                    print(f"Failed to parse eToro XLSX {filepath}: {e}")
                    parsing_errors.append({"file": os.path.basename(filepath), "error": err_msg})
            else:
                try:
                    txs = parse_xtb_xlsx(filepath)
                    transactions.extend(txs)
                    parsed_files += 1
                    source_counts["XTB"] += 1
                except Exception as e:
                    err_msg = str(e)
                    if "does not support the old .xls" in err_msg or ext == ".xls":
                        err_msg = "Excel 97-2003 (.xls) format is not supported by openpyxl. Please convert the file to .xlsx or .csv."
                    print(f"Failed to parse XTB XLSX {filepath}: {e}")
                    parsing_errors.append({"file": os.path.basename(filepath), "error": err_msg})
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
                elif is_trading212_file(filepath):
                    txs = parse_trading212_csv(filepath)
                    transactions.extend(txs)
                    parsed_files += 1
                    source_counts["Trading212"] += 1
                elif is_etoro_file(filepath):
                    txs = parse_etoro(filepath)
                    transactions.extend(txs)
                    parsed_files += 1
                    source_counts["eToro"] += 1
                elif "Open time" in sample and "Gross P/L" in sample:
                    msg = "XTB CSV detected; please export as XLSX for best compatibility or implement CSV xtb parser"
                    print(f"{msg} ({filepath})")
                    parsing_errors.append({"file": os.path.basename(filepath), "error": msg})
                else:
                    msg = "Unknown CSV format: Headers did not match Bitpanda, Trading212 or eToro templates"
                    print(f"{msg}: {filepath}")
                    parsing_errors.append({"file": os.path.basename(filepath), "error": msg})
            except Exception as e:
                print(f"Failed to parse CSV {filepath}: {e}")
                parsing_errors.append({"file": os.path.basename(filepath), "error": str(e)})
        elif ext not in [".txt", ".md"]: # Skip readme/documentation files
            msg = f"Unsupported file type '{ext}'"
            parsing_errors.append({"file": os.path.basename(filepath), "error": msg})
                
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
    trigger_pipeline()
    
    sources_loaded = []
    if source_counts["XTB"] > 0: sources_loaded.append("XTB")
    if source_counts["Bitpanda"] > 0: sources_loaded.append("Bitpanda")
    if source_counts["eToro"] > 0: sources_loaded.append("eToro")
    if source_counts["Trading212"] > 0: sources_loaded.append("Trading212")
    
    return render_template(
        "dashboard.html",
        kpis=cached_data["kpis"],
        positions=cached_data["positions"],
        transactions=cached_data["transactions"],
        charts=cached_data["charts"],
        allocation=cached_data["allocation"],
        dividends=cached_data["dividends"],
        risk=cached_data["risk"],
        analytics=cached_data["analytics"],
        source_breakdown=cached_data["source_breakdown"],
        last_updated=last_updated,
        sources_loaded=sources_loaded,
        parsed_files=parsed_files,
        parsing_errors=parsing_errors
    )

@app.route("/api/reload", methods=["POST"])
def reload_data():
    trigger_pipeline()
    pos_count = len(cached_data["positions"])
    return jsonify({
        "status": "success",
        "transactions_count": total_transactions,
        "positions_count": pos_count,
        "last_updated": last_updated,
        "parsing_errors": parsing_errors
    })

@app.route("/api/data", methods=["GET"])
def get_data():
    if cached_data is None:
        trigger_pipeline()
    return jsonify(cached_data)

if __name__ == "__main__":
    app.run(debug=True, port=5050)
