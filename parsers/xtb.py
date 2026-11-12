"""
XTB statement parser (xlsx)
===========================

XTB has shipped two export layouts; both are supported.

OLD layout (sheets "CLOSED POSITION HISTORY", "OPEN POSITION <ddmmyyyy>",
"CASH OPERATION HISTORY"): an account block (Name and surname / Account /
Currency / Balance) above each table, a period cell "01/01/2006 - 14/06/2026",
and times in XTB's local time (Europe/Warsaw, CET/CEST).

NEW layout (sheets "Closed Positions", "Cash Operations", "Open Positions"):
"Account number" in A1, "Date from (UTC)" / "Date to (UTC)" (or "Data as of
report generated") rows, and all times in UTC. Open Positions groups rows per
instrument: a summary row (empty Type, Instrument/Position = name) followed by
one row per position (Type = BUY, Instrument/Position = position id).

Assumptions shared by both layouts:
  * Open / close / market prices are in the INSTRUMENT currency (USD for .US
    listings), but Purchase value, Sale value, P/L, commission and every cash
    amount are in the account currency (EUR). Trade prices are therefore
    derived from the EUR values: price = Purchase (Sale) value / Volume.
    New layout open positions: EUR purchase value = Value - Gross Profit.
  * Realized P/L (net) = Gross P/L - |Commission| + Swap + Rollover (old), or
    the "Profit/Loss" column (new). Swap/rollover are booked as a separate cash
    row so that cash and realized P/L stay consistent.
  * Cash operations of trade types (stock purchase/sale, close trade, swap,
    rollover) are skipped: the position sheets already book them.
  * Open-position rows are snapshot legs (snapshot=True): the loader keeps only
    the newest snapshot per account.
  * Short (SELL-type) closed positions are booked as BUY+SELL at the close time
    with the SELL price chosen so the cash impact equals the realized P/L.
    Open shorts cannot be represented and are skipped with a warning.
"""

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import openpyxl

from parsers.base import (Statement, create_transaction, is_empty, norm_id,
                          safe_float, strip_exchange_suffix, to_naive_utc)

BROKER = "XTB"
XTB_LOCAL_TZ = ZoneInfo("Europe/Warsaw")

OLD_SHEETS = ("CLOSED POSITION HISTORY", "CASH OPERATION HISTORY")
NEW_SHEETS = ("closed positions", "cash operations", "open positions")

# Cash-operation types already covered by the positions sheets
_TRADE_CASH_KEYWORDS = ("purchase", "sale", "close trade", "open trade", "swap", "rollover")


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

_OLD_OPEN_SHEET = re.compile(r"^OPEN POSITION \d{8}$")   # case-sensitive on purpose


def _layout(wb) -> str:
    names = [s.strip() for s in wb.sheetnames]
    # Old XTB sheet names are upper-case; eToro's "Open Positions" must not match
    if any(s in names for s in OLD_SHEETS) or any(_OLD_OPEN_SHEET.match(s) for s in names):
        return "old"
    lower = [s.lower() for s in names]
    if "cash operations" in lower:
        return "new"
    first = wb[wb.sheetnames[0]]
    a1 = next(first.iter_rows(min_row=1, max_row=1, max_col=1, values_only=True), (None,))[0]
    if isinstance(a1, str) and a1.strip().lower() == "account number" and \
            any(s in lower for s in NEW_SHEETS):
        return "new"
    return ""


def detect(filepath: str) -> bool:
    if not filepath.lower().endswith(".xlsx"):
        return False
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    except Exception:
        return False
    try:
        return bool(_layout(wb))
    finally:
        wb.close()


# --------------------------------------------------------------------------
# Generic sheet helpers
# --------------------------------------------------------------------------

def _norm(h) -> str:
    return re.sub(r"\s+", " ", str(h or "")).strip().casefold()


def _rows(ws) -> list:
    return list(ws.iter_rows(values_only=True))


def _header_index(rows, required, max_scan=40) -> int:
    req = [_norm(r) for r in required]
    for i, row in enumerate(rows[:max_scan]):
        cells = {_norm(c) for c in row if c is not None}
        if all(r in cells for r in req):
            return i
    return -1


def _table(rows, required) -> list:
    """Rows below the header as dicts keyed by normalized header name."""
    hi = _header_index(rows, required)
    if hi < 0:
        return []
    header = [_norm(c) for c in rows[hi]]
    out = []
    for row in rows[hi + 1:]:
        if not any(not is_empty(c) for c in row):
            continue
        out.append({h: row[i] for i, h in enumerate(header) if h and i < len(row)})
    return out


def _label_value(rows, label, max_scan=15):
    """Value under a label cell in the account block (value is one row below)."""
    target = _norm(label)
    for i, row in enumerate(rows[:max_scan]):
        for j, c in enumerate(row):
            if _norm(c) == target and i + 1 < len(rows) and j < len(rows[i + 1]):
                return rows[i + 1][j]
    return None


def _label_right(rows, label, max_scan=15):
    """Value to the right of a label cell (new layout: 'Account number | 2162380')."""
    target = _norm(label)
    for row in rows[:max_scan]:
        for j, c in enumerate(row):
            if _norm(c) == target:
                for v in row[j + 1:]:
                    if not is_empty(v):
                        return v
    return None


_LOCAL_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M:%S",
                  "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S")


def _local_to_utc(val) -> datetime:
    """Old-layout times are Europe/Warsaw wall-clock times."""
    if isinstance(val, str):
        s = val.strip()
        for fmt in _LOCAL_FORMATS:
            try:
                val = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if not isinstance(val, datetime):
        raise ValueError(f"unparseable XTB time {val!r}")
    if val.tzinfo is None:
        val = val.replace(tzinfo=XTB_LOCAL_TZ)
    return to_naive_utc(val)


# --------------------------------------------------------------------------
# Transaction builders (shared by both layouts)
# --------------------------------------------------------------------------

class _Ctx:
    """Per-file state: statement + counters for warnings."""

    def __init__(self, st: Statement):
        self.st = st
        self.other_cash = {}
        self.skipped_open_shorts = 0

    def tx(self, **kw):
        kw.setdefault("source", BROKER)
        kw.setdefault("account", self.st.account)
        self.st.transactions.append(create_transaction(**kw))


def _price(value_eur: float, vol: float) -> float:
    return value_eur / vol if vol > 0 else 0.0


def _add_closed(ctx: _Ctx, *, pid, symbol, side, vol, open_t, close_t,
                purchase_eur, sale_eur, commission, swap_roll, realized, name):
    if vol <= 0 or not symbol:
        return
    ticker = strip_exchange_suffix(symbol)
    key = f"xtb:{ctx.st.account}:pos:{pid}"
    stamp = f"{close_t.isoformat()}:{vol:.8g}"
    fee = abs(commission)
    common = dict(ticker=ticker, qty=vol, position_id=pid, raw_symbol=symbol, name=name)
    buy_price = _price(purchase_eur, vol)
    if side == "SELL":
        # Short: both legs at close so no inverted exposure is ever valued
        sell_price = buy_price + (realized + fee) / vol
        buy_date = close_t
    else:
        sell_price = _price(sale_eur, vol)
        buy_date = open_t
    ctx.tx(date=buy_date, tx_type="BUY", price=buy_price,
           uid=f"{key}:buy:{stamp}", **common)
    ctx.tx(date=close_t, tx_type="SELL", price=max(sell_price, 0.0), fee=fee,
           realized_pnl=realized, uid=f"{key}:sell:{stamp}", **common)
    if abs(swap_roll) > 1e-9:
        ctx.tx(date=close_t, ticker=ticker, tx_type="INTEREST" if swap_roll > 0 else "FEE",
               qty=abs(swap_roll), price=1.0, position_id=pid, raw_symbol=symbol,
               uid=f"{key}:swap:{stamp}")


def _add_open(ctx: _Ctx, *, pid, symbol, side, vol, open_t, purchase_eur,
              market_eur, commission, name):
    if vol <= 0 or not symbol:
        return
    if side == "SELL":
        ctx.skipped_open_shorts += 1
        return
    ctx.tx(date=open_t, ticker=strip_exchange_suffix(symbol), tx_type="BUY", qty=vol,
           price=_price(purchase_eur, vol), fee=abs(commission), position_id=pid,
           market_price_at_export=_price(market_eur, vol) if market_eur is not None else None,
           raw_symbol=symbol, name=name, snapshot=True,
           uid=f"xtb:{ctx.st.account}:pos:{pid}:open")


def _cash_type(op_type: str, amount: float):
    """Map an XTB cash-operation type to (tx_type, is_known) or (None, True)
    for rows that must be skipped."""
    t = op_type.strip().lower()
    if not t:
        return None, True
    if any(k in t for k in _TRADE_CASH_KEYWORDS):
        return None, True
    if "deposit" in t:
        return "DEPOSIT", True
    if "withdraw" in t:
        return "WITHDRAWAL", True
    if "withholding" in t or ("interest" in t and "tax" in t):
        return "WITHHOLDING_TAX", True   # interest tax checked before interest
    if "divident" in t or "dividend" in t:
        return "DIVIDEND", True
    if "interest" in t:
        return "INTEREST", True
    if "fee" in t:
        return "FEE", True
    if abs(amount) < 1e-12:
        return None, True
    return ("INTEREST" if amount > 0 else "FEE"), False


def _add_cash(ctx: _Ctx, *, op_id, op_type, when, amount, symbol):
    tx_type, known = _cash_type(op_type, amount)
    if tx_type is None:
        return
    if not known:
        key = op_type.strip().lower()
        ctx.other_cash[key] = ctx.other_cash.get(key, 0) + 1
    holding_types = ("DIVIDEND", "WITHHOLDING_TAX", "FEE", "INTEREST")
    ticker = strip_exchange_suffix(symbol) if symbol and tx_type in holding_types else "CASH"
    if tx_type == "WITHHOLDING_TAX" and "interest" in op_type.lower():
        ticker = "CASH"
    if tx_type == "INTEREST" and known:
        ticker = "CASH"
    ctx.tx(date=when, ticker=ticker, tx_type=tx_type, qty=abs(amount), price=1.0,
           raw_symbol=symbol or None, uid=f"xtb:{ctx.st.account}:cash:{op_id}" if op_id else None)


def _finish_warnings(ctx: _Ctx):
    if ctx.other_cash:
        listed = ", ".join(f"{k} ×{n}" for k, n in sorted(ctx.other_cash.items()))
        ctx.st.warnings.append(
            f"{sum(ctx.other_cash.values())} cash operation(s) of unrecognised type ({listed}) "
            "were booked by sign as INTEREST (positive) or FEE (negative).")
    if ctx.skipped_open_shorts:
        ctx.st.warnings.append(
            f"{ctx.skipped_open_shorts} open short position(s) skipped: open shorts are not supported.")


# --------------------------------------------------------------------------
# OLD layout
# --------------------------------------------------------------------------

_PERIOD_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")


def _old_metadata(st: Statement, rows):
    st.account = st.account or norm_id(_label_value(rows, "Account"))
    holder = _label_value(rows, "Name and surname")
    st.holder = st.holder or (None if is_empty(holder) else str(holder).strip())
    bal = _label_value(rows, "Balance")
    if st.reported_cash is None and not is_empty(bal):
        st.reported_cash = safe_float(bal)
    if st.generated_at is None:
        for row in rows[:12]:
            stamp = next((c for c in row if isinstance(c, datetime) and c.hour + c.minute + c.second), None)
            if stamp:
                st.generated_at = _local_to_utc(stamp)
                break
    if st.period_start is None:
        for row in rows[:14]:
            m = next((_PERIOD_RE.search(c) for c in row if isinstance(c, str) and _PERIOD_RE.search(c)), None)
            if m:
                start = datetime.strptime(m.group(1), "%d/%m/%Y")
                end = datetime.strptime(m.group(2), "%d/%m/%Y") + timedelta(days=1) - timedelta(seconds=1)
                st.period_start = _local_to_utc(start)
                st.period_end = _local_to_utc(end)
                break


def _parse_old(wb, st: Statement):
    ctx = _Ctx(st)
    sheets = {s.strip().upper(): wb[s] for s in wb.sheetnames}

    closed_rows = _rows(sheets["CLOSED POSITION HISTORY"]) if "CLOSED POSITION HISTORY" in sheets else []
    cash_rows = _rows(sheets["CASH OPERATION HISTORY"]) if "CASH OPERATION HISTORY" in sheets else []
    open_name = next((s for s in sheets if s.startswith("OPEN POSITION")), None)
    open_rows = _rows(sheets[open_name]) if open_name else []
    for rows in (closed_rows, cash_rows, open_rows):
        if rows:
            _old_metadata(st, rows)
    if st.period_end and st.generated_at:
        st.period_end = min(st.period_end, st.generated_at)

    for r in _table(closed_rows, ["Position", "Symbol", "Open time"]):
        pid = norm_id(r.get("position"))
        if not pid or not isinstance(r.get("open time"), datetime) or not isinstance(r.get("close time"), datetime):
            continue  # summary ("Total") or malformed row
        commission = safe_float(r.get("commission"))
        swap_roll = safe_float(r.get("swap")) + safe_float(r.get("rollover"))
        realized = safe_float(r.get("gross p/l")) - abs(commission) + swap_roll
        _add_closed(ctx, pid=pid, symbol=str(r.get("symbol") or "").strip(),
                    side=str(r.get("type") or "BUY").strip().upper(),
                    vol=safe_float(r.get("volume")),
                    open_t=_local_to_utc(r["open time"]), close_t=_local_to_utc(r["close time"]),
                    purchase_eur=safe_float(r.get("purchase value")),
                    sale_eur=safe_float(r.get("sale value")),
                    commission=commission, swap_roll=swap_roll, realized=realized, name=None)

    if open_rows:
        st.snapshot_at = st.generated_at
        for r in _table(open_rows, ["Position", "Symbol", "Open time"]):
            pid = norm_id(r.get("position"))
            if not pid or not isinstance(r.get("open time"), datetime):
                continue
            purchase = safe_float(r.get("purchase value"))
            _add_open(ctx, pid=pid, symbol=str(r.get("symbol") or "").strip(),
                      side=str(r.get("type") or "BUY").strip().upper(),
                      vol=safe_float(r.get("volume")), open_t=_local_to_utc(r["open time"]),
                      purchase_eur=purchase,
                      market_eur=purchase + safe_float(r.get("gross p/l")),
                      commission=safe_float(r.get("commission")), name=None)

    for r in _table(cash_rows, ["ID", "Type", "Time", "Amount"]):
        op_id = norm_id(r.get("id"))
        when = r.get("time")
        if not op_id or not isinstance(when, datetime):
            continue  # "Total" row
        _add_cash(ctx, op_id=op_id, op_type=str(r.get("type") or ""),
                  when=_local_to_utc(when), amount=safe_float(r.get("amount")),
                  symbol=str(r.get("symbol") or "").strip())
    _finish_warnings(ctx)


# --------------------------------------------------------------------------
# NEW layout
# --------------------------------------------------------------------------

def _utc(val) -> datetime:
    return to_naive_utc(val)


def _new_metadata(st: Statement, rows):
    st.account = st.account or norm_id(_label_right(rows, "Account number"))
    start = _label_right(rows, "Date from (UTC)")
    end = _label_right(rows, "Date to (UTC)")
    as_of = _label_right(rows, "Data as of report generated")
    if start is not None and st.period_start is None:
        st.period_start = _utc(start)
    if end is not None:
        st.period_end = max(filter(None, [st.period_end, _utc(end)]))
    if as_of is not None:
        st.snapshot_at = _utc(as_of)
    st.generated_at = max(filter(None, [st.generated_at, st.period_end, st.snapshot_at]), default=None)


def _parse_new(wb, st: Statement):
    ctx = _Ctx(st)
    sheets = {s.strip().lower(): wb[s] for s in wb.sheetnames}
    rows_by = {k: _rows(sheets[k]) for k in NEW_SHEETS if k in sheets}
    for rows in rows_by.values():
        _new_metadata(st, rows)

    for r in _table(rows_by.get("closed positions", []), ["Ticker", "Volume", "Position ID"]):
        pid = norm_id(r.get("position id"))
        if not pid or is_empty(r.get("open time (utc)")) or is_empty(r.get("close time (utc)")):
            continue  # summary row
        commission = safe_float(r.get("commission"))
        swap_roll = safe_float(r.get("swap")) + safe_float(r.get("rollover"))
        if is_empty(r.get("profit/loss")):
            realized = safe_float(r.get("gross profit")) - abs(commission) + swap_roll
        else:
            realized = safe_float(r.get("profit/loss"))
        _add_closed(ctx, pid=pid, symbol=str(r.get("ticker") or "").strip(),
                    side=str(r.get("type") or "BUY").strip().upper(),
                    vol=safe_float(r.get("volume")),
                    open_t=_utc(r["open time (utc)"]), close_t=_utc(r["close time (utc)"]),
                    purchase_eur=safe_float(r.get("purchase value")),
                    sale_eur=safe_float(r.get("sale value")),
                    commission=commission, swap_roll=swap_roll, realized=realized,
                    name=r.get("instrument"))

    instrument_name = None
    for r in _table(rows_by.get("open positions", []), ["Instrument/Position", "Ticker", "Open time (UTC)"]):
        side = str(r.get("type") or "").strip().upper()
        label = r.get("instrument/position")
        if not side:
            instrument_name = None if is_empty(label) else str(label).strip()
            continue
        if is_empty(r.get("open time (utc)")):
            continue
        value = safe_float(r.get("value"))
        _add_open(ctx, pid=norm_id(label), symbol=str(r.get("ticker") or "").strip(), side=side,
                  vol=safe_float(r.get("volume")), open_t=_utc(r["open time (utc)"]),
                  purchase_eur=value - safe_float(r.get("gross profit")), market_eur=value,
                  commission=safe_float(r.get("open commission")), name=instrument_name)

    for r in _table(rows_by.get("cash operations", []), ["Type", "Time", "Amount", "ID"]):
        op_id = norm_id(r.get("id"))
        if not op_id or is_empty(r.get("time")):
            continue  # "Total" row
        _add_cash(ctx, op_id=op_id, op_type=str(r.get("type") or ""), when=_utc(r["time"]),
                  amount=safe_float(r.get("amount")), symbol=str(r.get("ticker") or "").strip())
    _finish_warnings(ctx)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse(filepath: str) -> Statement:
    st = Statement(broker=BROKER, file=os.path.basename(filepath))
    wb = openpyxl.load_workbook(filepath, data_only=True)
    try:
        layout = _layout(wb)
        if layout == "old":
            _parse_old(wb, st)
        elif layout == "new":
            _parse_new(wb, st)
        else:
            raise ValueError("not an XTB statement")
    finally:
        wb.close()
    return st
