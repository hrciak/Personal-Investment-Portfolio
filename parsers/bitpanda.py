import pandas as pd
import io
import csv
from parsers.base import create_transaction

def parse_bitpanda_csv(filepath: str) -> list[dict]:
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    start_idx = 0
    for i, line in enumerate(lines[:10]):
        if "Transaction ID" in line:
            start_idx = i
            break
            
    csv_data = "".join(lines[start_idx:])
    
    df = pd.read_csv(io.StringIO(csv_data), delimiter=',', quoting=csv.QUOTE_MINIMAL)
    transactions = []
    
    for _, row in df.iterrows():
        tx_type_raw = str(row.get("Transaction Type", "")).lower()
        in_out = str(row.get("In/Out", "")).lower()
        asset_class = str(row.get("Asset class", ""))
        
        # Timestamp parsing
        dt = pd.to_datetime(row.get("Timestamp"), utc=True).tz_convert(None).to_pydatetime()
        
        amount_asset = float(row.get("Amount Asset", 0.0)) if pd.notna(row.get("Amount Asset")) and row.get("Amount Asset") != "-" else 0.0
        amount_fiat = float(row.get("Amount Fiat", 0.0)) if pd.notna(row.get("Amount Fiat")) and row.get("Amount Fiat") != "-" else 0.0
        asset_price = float(row.get("Asset market price", 0.0)) if pd.notna(row.get("Asset market price")) and row.get("Asset market price") != "-" else 0.0
        
        asset = str(row.get("Asset", ""))
        
        if tx_type_raw == "buy":
            transactions.append(create_transaction(dt, asset, "BUY", amount_asset, asset_price, fee=0.0, source="Bitpanda"))
        elif tx_type_raw == "sell":
            transactions.append(create_transaction(dt, asset, "SELL", amount_asset, asset_price, fee=0.0, source="Bitpanda"))
        elif tx_type_raw == "deposit":
            transactions.append(create_transaction(dt, "CASH", "DEPOSIT", amount_fiat, 1.0, fee=0.0, source="Bitpanda"))
        elif tx_type_raw == "withdrawal":
            transactions.append(create_transaction(dt, "CASH", "WITHDRAWAL", amount_fiat, 1.0, fee=0.0, source="Bitpanda"))
        elif tx_type_raw in ("savings", "staking"):
            transactions.append(create_transaction(dt, asset, "DIVIDEND", amount_asset, asset_price, source="Bitpanda"))
        elif tx_type_raw in ("-", ""):
            if in_out == "incoming" and asset_class == "Fiat":
                transactions.append(create_transaction(dt, "CASH", "DEPOSIT", amount_fiat, 1.0, fee=0.0, source="Bitpanda"))
            elif asset == "EUR" and str(row.get("Amount Asset", "")) == "-":
                # Fallback for old deposits sometimes mapped this way
                transactions.append(create_transaction(dt, "CASH", "DEPOSIT", amount_fiat, 1.0, fee=0.0, source="Bitpanda"))

    return transactions
