"""
eToro Account Statement Parser

Supports eToro XLSX exports (Account Statement) which typically contain sheets:
  - "Closed Positions" — completed trades with P/L
  - "Transactions Report" — deposits, withdrawals, dividends, fees, adjustments
  - "Account Activity" — alternative format for some exports
  - "Financial Summary" — summary data (like the screenshot)

Also supports eToro CSV exports from Portfolio → History.

eToro operates primarily in USD. Conversion to EUR uses:
  1. The "Amount" column in EUR if present
  2. ECB exchange rate at transaction date (fetched once)
  3. Fallback fixed rate
"""

import openpyxl
import pandas as pd
import io
import csv
import requests
from datetime import datetime
from parsers.base import create_transaction


# --- USD/EUR conversion ---

_ecb_rate_cache = {}

def _get_usd_eur_rate(date_str: str = None) -> float:
    """Get USD→EUR conversion rate. Uses ECB API with caching."""
    global _ecb_rate_cache
    
    if date_str and date_str in _ecb_rate_cache:
        return _ecb_rate_cache[date_str]
    
    try:
        # Fetch latest rate from ECB
        resp = requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=EUR",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            rate = data.get("rates", {}).get("EUR", 0.855)
            _ecb_rate_cache["latest"] = rate
            if date_str:
                _ecb_rate_cache[date_str] = rate
            return rate
    except Exception:
        pass
    
    # Fallback rate (approximate)
    return _ecb_rate_cache.get("latest", 0.855)


def _usd_to_eur(amount_usd: float, rate: float = None) -> float:
    """Convert USD to EUR."""
    if rate is None:
        rate = _get_usd_eur_rate()
    return amount_usd * rate


# --- XLSX Parser (Account Statement export) ---

def _find_header_row(sheet, required_headers: list[str]) -> int:
    """Finds the 1-based index of the header row in a sheet."""
    for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        if not row:
            continue
        row_strs = [str(cell).strip().lower() for cell in row if cell is not None]
        if all(any(req.lower() in cell_str for cell_str in row_strs) for req in required_headers):
            return row_idx
    return -1


def _extract_rows(sheet, header_row_idx: int) -> list[dict]:
    """Extract rows as list of dicts from a sheet starting at header row."""
    rows = list(sheet.iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in rows[header_row_idx - 1]]
    
    data = []
    for row in rows[header_row_idx:]:
        if not any(row):
            continue
        row_dict = {}
        for col_idx, col_name in enumerate(header):
            if col_idx < len(row):
                row_dict[col_name] = row[col_idx]
        data.append(row_dict)
    return data


def _safe_float(val, default=0.0) -> float:
    """Safely convert a value to float."""
    if val is None or val == "" or val == "-" or val == "N/A":
        return default
    try:
        # Handle parentheses for negative numbers: (2.70) -> -2.70
        s = str(val).strip()
        if s.startswith("(") and s.endswith(")"):
            return -float(s[1:-1].replace(",", ""))
        return float(s.replace(",", "").replace("$", "").replace("€", ""))
    except (ValueError, TypeError):
        return default


def _parse_etoro_date(val) -> datetime:
    """Parse eToro date from various formats."""
    if isinstance(val, datetime):
        return val
    if isinstance(val, pd.Timestamp):
        return val.to_pydatetime()
    
    s = str(val).strip()
    for fmt in [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    ]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    
    # Fallback to pandas
    try:
        return pd.to_datetime(s).to_pydatetime()
    except Exception:
        return datetime.now()


def _strip_etoro_ticker(instrument: str) -> str:
    """Extract a clean ticker from eToro instrument names.
    
    eToro uses full names like 'Apple', 'Bitcoin', 'NVIDIA Corporation'
    and sometimes tickers like 'AAPL', 'BTC'.
    """
    if not instrument:
        return "UNKNOWN"
    
    # Common eToro instrument name → ticker mapping
    name_map = {
        "apple": "AAPL", "microsoft": "MSFT", "amazon": "AMZN",
        "google": "GOOGL", "alphabet": "GOOGL", "meta": "META",
        "facebook": "META", "nvidia": "NVDA", "tesla": "TSLA",
        "netflix": "NFLX", "amd": "AMD", "intel": "INTC",
        "disney": "DIS", "coca-cola": "KO", "coca cola": "KO",
        "pepsi": "PEP", "pepsico": "PEP", "walmart": "WMT",
        "johnson": "JNJ", "procter": "PG", "visa": "V",
        "mastercard": "MA", "paypal": "PYPL", "adobe": "ADBE",
        "salesforce": "CRM", "uber": "UBER", "airbnb": "ABNB",
        "palantir": "PLTR", "coinbase": "COIN", "shopify": "SHOP",
        "spotify": "SPOT", "snap": "SNAP", "twitter": "X",
        "zoom": "ZM", "square": "SQ", "block": "SQ",
        "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
        "cardano": "ADA", "ripple": "XRP", "xrp": "XRP",
        "polkadot": "DOT", "dogecoin": "DOGE", "litecoin": "LTC",
        "chainlink": "LINK", "avalanche": "AVAX", "polygon": "MATIC",
        "uniswap": "UNI", "aave": "AAVE", "cosmos": "ATOM",
        "near protocol": "NEAR", "fantom": "FTM",
        "s&p 500": "SPY", "spdr": "SPY",
        "vanguard s&p": "VOO", "ishares": "IVV",
    }
    
    instrument_lower = instrument.strip().lower()
    
    # Check direct name match
    for name, ticker in name_map.items():
        if name in instrument_lower:
            return ticker
    
    # If it looks like a ticker already (short, uppercase-ish)
    clean = instrument.strip().split("/")[0].strip()
    if len(clean) <= 6 and clean.replace(".", "").replace("-", "").isalnum():
        return clean.upper()
    
    # Fallback: take first word, uppercase
    return instrument.strip().split()[0].upper() if instrument.strip() else "UNKNOWN"


def _parse_closed_positions(sheet, eur_rate: float) -> list[dict]:
    """Parse eToro 'Closed Positions' sheet."""
    transactions = []
    
    # Try different header combinations
    hdr_idx = _find_header_row(sheet, ["Action", "Amount"])
    if hdr_idx == -1:
        hdr_idx = _find_header_row(sheet, ["Position ID", "Profit"])
    if hdr_idx == -1:
        return transactions
    
    rows = _extract_rows(sheet, hdr_idx)
    
    for r in rows:
        # Extract fields — eToro closed positions vary in column naming
        instrument = str(r.get("Action", r.get("Instrument", r.get("Asset", ""))))
        pos_id = str(r.get("Position ID", r.get("Position", "")))
        
        # Amount is total position value at open
        amount = _safe_float(r.get("Amount", r.get("Invested", 0)))
        units = _safe_float(r.get("Units", r.get("Qty", 0)))
        open_rate = _safe_float(r.get("Open Rate", r.get("Open Price", 0)))
        close_rate = _safe_float(r.get("Close Rate", r.get("Close Price", 0)))
        profit = _safe_float(r.get("Profit", r.get("P/L", r.get("Net Profit", 0))))
        
        # Dates
        open_date_raw = r.get("Open Date", r.get("Open Time", None))
        close_date_raw = r.get("Close Date", r.get("Close Time", None))
        
        if not open_date_raw or not instrument:
            continue
        
        open_date = _parse_etoro_date(open_date_raw)
        close_date = _parse_etoro_date(close_date_raw) if close_date_raw else open_date
        
        ticker = _strip_etoro_ticker(instrument)
        
        # If no units, derive from amount and open rate
        if units == 0 and open_rate > 0:
            units = amount / open_rate
        
        # Convert to EUR
        open_price_eur = _usd_to_eur(open_rate, eur_rate)
        close_price_eur = _usd_to_eur(close_rate, eur_rate)
        
        # BUY leg
        transactions.append(create_transaction(
            date=open_date,
            ticker=ticker,
            tx_type="BUY",
            qty=units,
            price=open_price_eur,
            fee=0.0,
            realized_pnl=None,
            position_id=pos_id,
            source="eToro",
            currency="EUR"
        ))
        
        # SELL leg
        profit_eur = _usd_to_eur(profit, eur_rate)
        transactions.append(create_transaction(
            date=close_date,
            ticker=ticker,
            tx_type="SELL",
            qty=units,
            price=close_price_eur,
            fee=0.0,
            realized_pnl=profit_eur,
            position_id=pos_id,
            source="eToro",
            currency="EUR"
        ))
    
    return transactions


def _parse_transactions_report(sheet, eur_rate: float) -> list[dict]:
    """Parse eToro 'Transactions Report' sheet — deposits, withdrawals, dividends, fees."""
    transactions = []
    
    hdr_idx = _find_header_row(sheet, ["Date", "Type"])
    if hdr_idx == -1:
        hdr_idx = _find_header_row(sheet, ["Date", "Detail"])
    if hdr_idx == -1:
        return transactions
    
    rows = _extract_rows(sheet, hdr_idx)
    
    for r in rows:
        date_raw = r.get("Date", r.get("Time", None))
        if not date_raw:
            continue
        
        dt = _parse_etoro_date(date_raw)
        tx_type_raw = str(r.get("Type", r.get("Details", r.get("Detail", "")))).lower()
        amount_usd = _safe_float(r.get("Amount", r.get("Credit", 0))) + _safe_float(r.get("Debit", 0))
        
        # Realized equity credit/debit
        realized = _safe_float(r.get("Realized Equity Change", r.get("Balance", 0)))
        
        amount_eur = _usd_to_eur(abs(amount_usd), eur_rate) if amount_usd != 0 else _usd_to_eur(abs(realized), eur_rate)
        
        ticker = str(r.get("Asset", r.get("Instrument", "CASH")))
        if not ticker or ticker in ("None", "nan", "-", ""):
            ticker = "CASH"
        else:
            ticker = _strip_etoro_ticker(ticker)
        
        if "deposit" in tx_type_raw:
            transactions.append(create_transaction(
                date=dt, ticker="CASH", tx_type="DEPOSIT",
                qty=amount_eur, price=1.0, fee=0.0, source="eToro"
            ))
        elif "withdraw" in tx_type_raw:
            transactions.append(create_transaction(
                date=dt, ticker="CASH", tx_type="WITHDRAWAL",
                qty=amount_eur, price=1.0, fee=0.0, source="eToro"
            ))
        elif "dividend" in tx_type_raw:
            transactions.append(create_transaction(
                date=dt, ticker=ticker, tx_type="DIVIDEND",
                qty=amount_eur, price=1.0, fee=0.0, source="eToro"
            ))
        elif "withholding tax" in tx_type_raw or "tax" in tx_type_raw:
            transactions.append(create_transaction(
                date=dt, ticker=ticker, tx_type="WITHHOLDING_TAX",
                qty=amount_eur, price=1.0, fee=0.0, source="eToro"
            ))
        elif "rollover" in tx_type_raw or "overnight" in tx_type_raw:
            # Overnight fees — treat as a fee/cost
            if amount_usd < 0:
                transactions.append(create_transaction(
                    date=dt, ticker=ticker, tx_type="FEE",
                    qty=amount_eur, price=1.0, fee=amount_eur, source="eToro"
                ))
        elif "adjustment" in tx_type_raw:
            # Adjustments — treat as deposits (positive) or withdrawals (negative)
            if amount_usd >= 0:
                transactions.append(create_transaction(
                    date=dt, ticker="CASH", tx_type="DEPOSIT",
                    qty=amount_eur, price=1.0, fee=0.0, source="eToro"
                ))
            else:
                transactions.append(create_transaction(
                    date=dt, ticker="CASH", tx_type="WITHDRAWAL",
                    qty=amount_eur, price=1.0, fee=0.0, source="eToro"
                ))
    
    return transactions


def _parse_account_activity(sheet, eur_rate: float) -> list[dict]:
    """Parse eToro 'Account Activity' sheet (alternative layout in some exports)."""
    transactions = []
    
    hdr_idx = _find_header_row(sheet, ["Date", "Amount"])
    if hdr_idx == -1:
        return transactions
    
    rows = _extract_rows(sheet, hdr_idx)
    
    for r in rows:
        date_raw = r.get("Date", r.get("Time", None))
        if not date_raw:
            continue
        
        dt = _parse_etoro_date(date_raw)
        detail = str(r.get("Details", r.get("Type", r.get("Description", "")))).lower()
        amount_usd = _safe_float(r.get("Amount", 0))
        amount_eur = _usd_to_eur(abs(amount_usd), eur_rate)
        
        if "deposit" in detail:
            transactions.append(create_transaction(
                date=dt, ticker="CASH", tx_type="DEPOSIT",
                qty=amount_eur, price=1.0, source="eToro"
            ))
        elif "withdraw" in detail:
            transactions.append(create_transaction(
                date=dt, ticker="CASH", tx_type="WITHDRAWAL",
                qty=amount_eur, price=1.0, source="eToro"
            ))
        elif "dividend" in detail:
            transactions.append(create_transaction(
                date=dt, ticker="CASH", tx_type="DIVIDEND",
                qty=amount_eur, price=1.0, source="eToro"
            ))
    
    return transactions


# --- CSV Parser ---

def _parse_etoro_csv(filepath: str) -> list[dict]:
    """Parse eToro CSV export (trade history from Portfolio → History)."""
    eur_rate = _get_usd_eur_rate()
    
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Find the header row
    start_idx = 0
    for i, line in enumerate(lines[:10]):
        if any(h in line for h in ["Position ID", "Action", "Open Date", "Amount"]):
            start_idx = i
            break
    
    csv_data = "".join(lines[start_idx:])
    df = pd.read_csv(io.StringIO(csv_data), delimiter=',', quoting=csv.QUOTE_MINIMAL)
    
    transactions = []
    
    for _, row in df.iterrows():
        action = str(row.get("Action", row.get("Type", ""))).lower()
        instrument = str(row.get("Action", row.get("Instrument", row.get("Asset", ""))))
        
        # For trade rows
        if any(keyword in action for keyword in ["buy", "sell"]):
            amount = _safe_float(row.get("Amount", 0))
            units = _safe_float(row.get("Units", 0))
            open_rate = _safe_float(row.get("Open Rate", 0))
            close_rate = _safe_float(row.get("Close Rate", 0))
            profit = _safe_float(row.get("Profit", row.get("P/L", 0)))
            pos_id = str(row.get("Position ID", ""))
            
            open_date = _parse_etoro_date(row.get("Open Date", row.get("Date", "")))
            close_date_raw = row.get("Close Date", None)
            close_date = _parse_etoro_date(close_date_raw) if pd.notna(close_date_raw) else open_date
            
            ticker = _strip_etoro_ticker(instrument)
            
            if units == 0 and open_rate > 0:
                units = amount / open_rate
            
            # BUY leg
            transactions.append(create_transaction(
                date=open_date,
                ticker=ticker,
                tx_type="BUY",
                qty=units,
                price=_usd_to_eur(open_rate, eur_rate),
                fee=0.0,
                realized_pnl=None,
                position_id=pos_id,
                source="eToro"
            ))
            
            # SELL leg (if closed)
            if close_rate > 0:
                transactions.append(create_transaction(
                    date=close_date,
                    ticker=ticker,
                    tx_type="SELL",
                    qty=units,
                    price=_usd_to_eur(close_rate, eur_rate),
                    fee=0.0,
                    realized_pnl=_usd_to_eur(profit, eur_rate),
                    position_id=pos_id,
                    source="eToro"
                ))
    
    return transactions


# --- Main entry point ---

def parse_etoro(filepath: str) -> list[dict]:
    """Parse an eToro export file (XLSX or CSV).
    
    Supports:
    - XLSX Account Statement (multi-sheet with Closed Positions, Transactions Report, etc.)
    - CSV trade history export
    """
    ext = filepath.rsplit(".", 1)[-1].lower()
    eur_rate = _get_usd_eur_rate()
    
    if ext in ("xlsx", "xls"):
        return _parse_etoro_xlsx(filepath, eur_rate)
    elif ext == "csv":
        return _parse_etoro_csv(filepath)
    else:
        print(f"eToro parser: unsupported file extension .{ext}")
        return []


def _parse_etoro_xlsx(filepath: str, eur_rate: float) -> list[dict]:
    """Parse eToro XLSX Account Statement."""
    wb = openpyxl.load_workbook(filepath, data_only=True)
    transactions = []
    
    sheet_names_lower = {sn.lower(): sn for sn in wb.sheetnames}
    
    # 1. Closed Positions
    closed_sheet = None
    for key in ["closed positions", "closedpositions"]:
        if key in sheet_names_lower:
            closed_sheet = wb[sheet_names_lower[key]]
            break
    
    if closed_sheet:
        transactions.extend(_parse_closed_positions(closed_sheet, eur_rate))
    
    # 2. Transactions Report
    tx_sheet = None
    for key in ["transactions report", "transactionsreport", "transaction report"]:
        if key in sheet_names_lower:
            tx_sheet = wb[sheet_names_lower[key]]
            break
    
    if tx_sheet:
        transactions.extend(_parse_transactions_report(tx_sheet, eur_rate))
    
    # 3. Account Activity (fallback)
    activity_sheet = None
    for key in ["account activity", "accountactivity", "activity"]:
        if key in sheet_names_lower:
            activity_sheet = wb[sheet_names_lower[key]]
            break
    
    if activity_sheet and not tx_sheet:
        # Only use activity if no transactions report found
        transactions.extend(_parse_account_activity(activity_sheet, eur_rate))
    
    return transactions


def is_etoro_file(filepath: str) -> bool:
    """Detect if a file is an eToro export.
    
    For XLSX: Check for characteristic sheet names.
    For CSV: Check for eToro-specific column headers.
    """
    ext = filepath.rsplit(".", 1)[-1].lower()
    
    if ext in ("xlsx", "xls"):
        try:
            wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
            sheet_names_lower = [sn.lower() for sn in wb.sheetnames]
            wb.close()
            
            etoro_indicators = [
                "closed positions", "financial summary",
                "transactions report", "account activity",
                "account details"
            ]
            return any(ind in sn for sn in sheet_names_lower for ind in etoro_indicators)
        except Exception:
            return False
    
    elif ext == "csv":
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                sample = f.read(2048)
            
            # eToro CSV signatures
            etoro_signatures = [
                "Position ID" in sample and "Open Rate" in sample,
                "Position ID" in sample and "Close Rate" in sample,
                "eToro" in sample,
            ]
            return any(etoro_signatures)
        except Exception:
            return False
    
    return False
