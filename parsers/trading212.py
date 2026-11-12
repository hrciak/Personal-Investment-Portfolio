"""
Trading 212 CSV statement parser
================================

Trading 212 exports a flat CSV (History -> Export). Columns include:
  Action, Time, ISIN, Ticker, Name, Notes, ID, No. of shares, Price / share,
  Currency (Price / share), Exchange rate, Result, Currency (Result), Total,
  Currency (Total), Withholding tax, Currency (Withholding tax), and one
  "<fee name>" + "Currency (<fee name>)" pair per fee/tax type.

Assumptions (verified on a real EUR export):
  * Times are UTC. "Total" is in the account currency (EUR).
  * On buys "Total" INCLUDES the itemized fees; on sells it is NET of them.
    The EUR unit price is therefore (Total -/+ fees) / shares and the fees are
    booked separately, so the engine's cost / proceeds equal "Total" exactly.
  * "Exchange rate" = instrument currency per EUR (GBX quotes are per pence).
    It converts withholding tax or fees reported in the instrument currency.
  * "Result" (realized P/L) EXCLUDES fees; the parser books it net of the
    sell's fees and of the buy fees allocated (pro rata) to the sold shares.
  * Dividends: "Total" is the NET amount credited; the gross is booked as
    DIVIDEND (net + withholding) plus a separate WITHHOLDING_TAX row.
  * "Free Shares Promotion" deposits, interest and cashback are income
    (INTEREST), not contributions. Card debits are withdrawals.
"""

import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

from parsers.base import Statement, create_transaction, is_empty, safe_float

BROKER = "Trading212"
_SIGNATURE = ("Action", "No. of shares", "Price / share", "Total")
_NOT_FEES = {"Price / share", "Result", "Total", "Withholding tax"}
_INCOME_ACTIONS = ("interest", "cashback")


def detect(filepath: str) -> bool:
    if not filepath.lower().endswith(".csv"):
        return False
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            header = f.readline()
    except OSError:
        return False
    return all(col in header for col in _SIGNATURE)


def _fee_columns(header: list) -> list:
    return [c for c in header
            if not c.startswith("Currency (") and f"Currency ({c})" in header
            and c not in _NOT_FEES and re.search(r"fee|tax", c, re.IGNORECASE)]


class _Row:
    """Accessors for one CSV row, including currency conversion to EUR."""

    def __init__(self, raw: dict, fee_cols: list):
        self.raw = raw
        self.fee_cols = fee_cols

    def text(self, col) -> str:
        return str(self.raw.get(col) or "").strip()

    def num(self, col) -> float:
        return safe_float(self.raw.get(col))

    def to_eur(self, amount: float, currency: str):
        """Convert an amount to EUR; None if the currency can't be converted."""
        ccy = currency.strip().upper()
        if not ccy or ccy == "EUR" or amount == 0:
            return amount
        rate = self.num("Exchange rate")
        price_ccy = self.text("Currency (Price / share)").upper()
        if rate > 0 and ccy == price_ccy:
            return amount / rate
        if rate > 0 and ccy == "GBP" and price_ccy == "GBX":
            return amount / (rate / 100.0)
        return None

    def fees_eur(self, warn) -> float:
        total = 0.0
        for col in self.fee_cols:
            amt = abs(self.num(col))
            eur = self.to_eur(amt, self.text(f"Currency ({col})"))
            if eur is None:
                warn(f"fee '{col}' in {self.text(f'Currency ({col})')} could not be converted; booked as EUR")
                eur = amt
            total += eur
        return total


def _period(st: Statement, filename: str):
    m = re.search(r"from_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})", filename)
    if m:
        st.period_start = datetime.strptime(m.group(1), "%Y-%m-%d")
        st.period_end = datetime.strptime(m.group(2), "%Y-%m-%d") + timedelta(days=1, seconds=-1)
        st.generated_at = st.period_end


def _classify(action: str, notes: str) -> str:
    a = action.lower()
    if "dividend" in a:
        return "DIVIDEND"
    if any(k in a for k in _INCOME_ACTIONS):
        return "INTEREST"
    if a == "deposit":
        return "INTEREST" if "promotion" in notes.lower() else "DEPOSIT"
    if "withdraw" in a or a == "card debit":
        return "WITHDRAWAL"
    if "buy" in a:
        return "BUY"
    if "sell" in a:
        return "SELL"
    if a == "currency conversion":
        return "SKIP"
    return ""


def parse(filepath: str) -> Statement:
    st = Statement(broker=BROKER, file=os.path.basename(filepath))
    _period(st, st.file)
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        raw_rows = list(reader)
    fee_cols = _fee_columns(header)

    issues = defaultdict(int)

    def warn(msg):
        issues[msg] += 1

    rows = sorted((_Row(r, fee_cols) for r in raw_rows if not is_empty(r.get("Action"))),
                  key=lambda r: r.text("Time"))
    held = defaultdict(lambda: [0.0, 0.0])   # ticker -> [shares, unallocated buy fees]
    for row in rows:
        _book_row(st, row, held, warn)

    for msg, n in issues.items():
        st.warnings.append(f"{msg} ({n} row(s)).")
    if st.transactions and st.period_end is None:
        st.period_start = min(t["date"] for t in st.transactions)
        st.period_end = st.generated_at = max(t["date"] for t in st.transactions)
    return st


def _book_row(st: Statement, row: _Row, held: dict, warn):
    action = row.text("Action")
    kind = _classify(action, row.text("Notes"))
    if kind == "SKIP":
        return
    if not kind:
        warn(f"unsupported action '{action}' skipped")
        return
    if row.text("Currency (Total)").upper() not in ("", "EUR"):
        warn(f"non-EUR account currency {row.text('Currency (Total)')} not supported; row skipped")
        return

    when = row.text("Time")
    tx_id = row.text("ID")
    uid = f"t212:{tx_id}" if tx_id else None
    total = abs(row.num("Total"))
    ticker = row.text("Ticker")
    isin = row.text("ISIN") or None
    name = row.text("Name") or None

    def add(**kw):
        kw.setdefault("source", BROKER)
        st.transactions.append(create_transaction(date=when, **kw))

    if kind in ("DEPOSIT", "WITHDRAWAL", "INTEREST"):
        add(ticker="CASH", tx_type=kind, qty=total, price=1.0, uid=uid)
        return
    if kind == "DIVIDEND":
        sym = ticker or "CASH"
        wht = abs(row.num("Withholding tax"))
        wht_eur = row.to_eur(wht, row.text("Currency (Withholding tax)"))
        if wht_eur is None:
            warn("withholding tax in an unconvertible currency booked as EUR")
            wht_eur = wht
        if total + wht_eur > 0:
            add(ticker=sym, tx_type="DIVIDEND", qty=total + wht_eur, price=1.0, isin=isin,
                name=name, uid=uid)
        if wht_eur > 0:
            add(ticker=sym, tx_type="WITHHOLDING_TAX", qty=wht_eur, price=1.0, isin=isin,
                name=name, uid=f"{uid}:wht" if uid else None)
        return

    shares = abs(row.num("No. of shares"))
    if not ticker or shares <= 0:
        warn(f"{action} row without ticker/shares skipped")
        return
    fees = row.fees_eur(warn)
    pos = held[ticker]
    if kind == "BUY":
        price = max(total - fees, 0.0) / shares
        add(ticker=ticker, tx_type="BUY", qty=shares, price=price, fee=fees, isin=isin,
            name=name, uid=uid)
        pos[0] += shares
        pos[1] += fees
        return

    # SELL
    price = (total + fees) / shares
    realized = None
    if not is_empty(row.raw.get("Result")):
        alloc = pos[1] * min(shares / pos[0], 1.0) if pos[0] > 0 else 0.0
        realized = row.num("Result") - fees - alloc
        pos[1] -= alloc
    pos[0] = max(pos[0] - shares, 0.0)
    add(ticker=ticker, tx_type="SELL", qty=shares, price=price, fee=fees,
        realized_pnl=realized, isin=isin, name=name, uid=uid)
