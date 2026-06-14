"""
Trading 212 CSV statement parser
================================

Trading 212 exports a flat CSV (Account → History → Export). Columns:
  Action, Time, ISIN, Ticker, Name, Notes, ID, No. of shares,
  Price / share, Currency (Price / share), Exchange rate, Result,
  Currency (Result), Total, Currency (Total), Withholding tax, ...fees

The account is EUR-denominated and the "Total" column is always in the account
currency (EUR), so we derive the per-share EUR price as Total / shares. This
avoids dealing with instrument currencies like GBX (pence) and the exchange
rate column. Realized P/L on sells comes straight from the "Result" column.
"""

import csv
import io
from datetime import datetime
from parsers.base import create_transaction

# Header columns that, together, identify a Trading 212 export
_SIGNATURE = ["Action", "No. of shares", "Price / share", "Total"]


def _f(val, default=0.0):
    if val is None:
        return default
    s = str(val).strip()
    if s == "" or s == "-":
        return default
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return default


def _parse_dt(val):
    s = str(val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    import pandas as pd
    return pd.to_datetime(s).to_pydatetime()


def _fees(row):
    """Sum the (non-withholding) costs Trading 212 itemizes per trade."""
    return (_f(row.get("Currency conversion fee"))
            + _f(row.get("Stamp duty reserve tax"))
            + _f(row.get("French transaction tax"))
            + _f(row.get("Finra fee")))


def is_trading212_file(filepath: str) -> bool:
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            header = f.readline()
        return all(col in header for col in _SIGNATURE)
    except Exception:
        return False


def parse_trading212_csv(filepath: str) -> list:
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    transactions = []
    for r in rows:
        action = str(r.get("Action", "")).strip().lower()
        if not action:
            continue

        dt = _parse_dt(r.get("Time"))
        ticker = str(r.get("Ticker", "")).strip()
        name = str(r.get("Name", "")).strip()
        shares = abs(_f(r.get("No. of shares")))
        total = abs(_f(r.get("Total")))
        # Per-share price in EUR (account currency), derived from the EUR total
        price_eur = (total / shares) if shares > 0 else 0.0

        if "deposit" in action:
            transactions.append(create_transaction(
                date=dt, ticker="CASH", tx_type="DEPOSIT", qty=total, price=1.0, source="Trading212"))

        elif "withdraw" in action:
            transactions.append(create_transaction(
                date=dt, ticker="CASH", tx_type="WITHDRAWAL", qty=total, price=1.0, source="Trading212"))

        elif "buy" in action:
            if not ticker:
                continue
            transactions.append(create_transaction(
                date=dt, ticker=ticker, tx_type="BUY", qty=shares, price=price_eur,
                fee=_fees(r), source="Trading212", isin=r.get("ISIN")))

        elif "sell" in action:
            if not ticker:
                continue
            result = r.get("Result")
            transactions.append(create_transaction(
                date=dt, ticker=ticker, tx_type="SELL", qty=shares, price=price_eur,
                fee=_fees(r),
                realized_pnl=_f(result) if result not in (None, "", "-") else None,
                source="Trading212", isin=r.get("ISIN")))

        elif "dividend" in action:
            sym = ticker or "CASH"
            wht = _f(r.get("Withholding tax"))
            # Trading 212 "Total" on a dividend is the net credited amount; record
            # the gross (net + withholding) plus a separate withholding-tax leg so
            # cash and the tax KPI both stay correct.
            gross = total + wht
            if gross > 0:
                transactions.append(create_transaction(
                    date=dt, ticker=sym, tx_type="DIVIDEND", qty=gross, price=1.0, source="Trading212"))
            if wht > 0:
                transactions.append(create_transaction(
                    date=dt, ticker=sym, tx_type="WITHHOLDING_TAX", qty=wht, price=1.0, source="Trading212"))

        elif "interest" in action:
            # Interest on cash — treat as a cash inflow so the balance stays right
            transactions.append(create_transaction(
                date=dt, ticker="CASH", tx_type="DEPOSIT", qty=total, price=1.0, source="Trading212"))

    return transactions
