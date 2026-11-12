"""
eToro account-statement parser (xlsx, plus the legacy history csv)
==================================================================

eToro's "Account Statement" workbook is localized to the account language;
English and Czech sheet / column names are supported (extend the keyword
tables below for more locales). Sheets used:

  details     "Account Summary" / "Account Details" / "Přehled účtu"
              (holder, username, creation / start / end dates)
  closed      "Closed Positions" / "Zavřené pozice"  -> BUY + SELL legs
  activity    "Account Activity" / "Aktivita na účtu" -> cash rows, and the
              "open position" events of positions still open at period end
              (the statement has NO open-positions sheet)
  dividends   "Dividends" / "Dividendy"

Assumptions:
  * Times are UTC. The account is USD-denominated; EUR figures from the
    statement (realized profit, the EUR amount in deposit details, dividend
    EUR columns) are used directly. USD-only amounts are converted with ONE
    statement-wide USD->EUR rate, derived from deposits (EUR sent vs. USD
    credited + the FX fee charged on it), else from closed-position profit
    (EUR vs. USD), else a live ECB rate, else a static fallback (warned).
  * A deposit is booked as the EUR amount sent; the FX conversion fee is a
    separate FEE row (the USD credit is already net of it).
  * SELL legs are priced so that proceeds - cost == the statement's EUR
    profit. Closed shorts are booked BUY+SELL at the close time.
  * Positions still open at period end come from activity "open position"
    rows (units, invested USD); leverage is assumed 1 for them.
  * Dividends come from the Dividends sheet when it has rows (net + tax ->
    gross DIVIDEND and a WITHHOLDING_TAX row), else from activity rows (net).
"""

import csv
import io
import os
import re
import statistics
from datetime import datetime

import openpyxl
import requests

from parsers.base import (Statement, create_transaction, is_empty, norm_id,
                          safe_float)

BROKER = "eToro"
STATIC_USD_EUR = 0.92

# --------------------------------------------------------------------------
# Localized keyword tables (normalized: lower-case, single spaces)
# --------------------------------------------------------------------------

SHEETS = {
    "details":   ["account summary", "account details", "přehled účtu", "prehled uctu"],
    "closed":    ["closed positions", "zavřené pozice", "zavrene pozice"],
    "activity":  ["account activity", "aktivita na účtu", "aktivita na uctu"],
    "dividends": ["dividends", "dividendy"],
    "summary":   ["financial summary", "finanční shrnutí", "financni shrnuti"],
}
# A workbook is eToro only if it has one of these (NOT "closed positions",
# which XTB's new layout also uses)
DETECT_SHEETS = SHEETS["details"] + SHEETS["activity"] + SHEETS["summary"]

COLS = {
    # closed positions
    "position_id": ["position id", "id pozice"],
    "action":      ["action", "akce"],
    "long_short":  ["long / short", "long/short"],
    "amount":      ["amount", "částka", "castka"],
    "units":       ["units", "jednotky"],
    "open_date":   ["open date", "datum otevření", "datum otevreni"],
    "close_date":  ["close date", "datum uzavření", "datum uzavreni"],
    "leverage":    ["leverage", "pákový efekt", "pakovy efekt"],
    "profit_eur":  ["profit (eur)", "zisk (eur)"],
    "profit_usd":  ["profit (usd)", "zisk (usd)", "profit", "zisk"],
    "open_rate":   ["open rate", "otevírací kurz", "oteviraci kurz"],
    "close_rate":  ["close rate", "uzavírací kurz", "uzaviraci kurz"],
    "isin":        ["isin"],
    # account activity
    "date":        ["date", "datum"],
    "row_type":    ["type", "napište", "napiste"],
    "details":     ["details", "podrobnosti"],
    # dividends
    "div_date":    ["date of payment", "payment date", "datum platby"],
    "div_name":    ["instrument name", "název nástroje", "nazev nastroje"],
    "div_net_eur": ["net dividend received (eur)", "čistá přijatá dividenda (eur)"],
    "div_net_usd": ["net dividend received (usd)", "čistá přijatá dividenda (usd)"],
    "div_wht_eur": ["withholding tax amount (eur)", "částka srážkové daně (eur)"],
    "div_wht_usd": ["withholding tax amount (usd)", "částka srážkové daně (usd)"],
}

DETAILS = {
    "holder":   ["name", "jméno"],
    "username": ["username", "user name", "uživatelské jméno"],
    "created":  ["date created", "datum vytvoření"],
    "start":    ["start date", "datum zahájení"],
    "end":      ["end date", "datum ukončení"],
}
_NOT_A_NAME = {"totals", "celkové částky"}

# Activity row types (substring keywords, checked in this order)
ACTIVITY_RULES = [
    ("skip",       ["zisk/ztráta", "zisk/ztrata", "profit/loss", "cancel"]),
    ("open",       ["otevřená pozice", "otevrena pozice", "open position"]),
    ("fee",        ["poplatek", "fee", "provize", "commission"]),
    ("by_sign",    ["úprava", "uprava", "adjustment", "rollover", "overnight", "jednodenní"]),
    ("interest",   ["refund", "refundace", "vratk", "interest", "úrok", "kredit", "credit"]),
    ("withdrawal", ["výběr", "vyber", "withdraw", "transfer to etoro money"]),
    ("deposit",    ["vklad", "deposit", "transfer to trading"]),
    ("dividend",   ["dividend"]),
]
_EUR_IN_DETAILS = re.compile(r"([\d.,]+)\s*EUR", re.IGNORECASE)
_SHORT_PREFIXES = ("sell ", "prodat ", "prodej ")
_LONG_PREFIXES = ("buy ", "koupit ", "nákup ", "nakup ")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold()


def resolve_column(headers, variants):
    """Index of the best header for `variants`: exact match first, then
    prefix, then substring (each pass in variant priority order)."""
    hs = [_norm(h) for h in headers]
    vs = [_norm(v) for v in variants]
    for test in (lambda h, v: h == v, lambda h, v: h.startswith(v), lambda h, v: v in h):
        for v in vs:
            for i, h in enumerate(hs):
                if h and test(h, v):
                    return i
    return None


class _Table:
    def __init__(self, header, rows):
        self.header = header
        self.rows = rows
        self._idx = {}

    def col(self, key):
        if key not in self._idx:
            self._idx[key] = resolve_column(self.header, COLS[key])
        return self._idx[key]

    def get(self, row, key, default=None):
        i = self.col(key)
        if i is None or i >= len(row) or is_empty(row[i]):
            return default
        return row[i]


def _table(ws, required, max_scan=15):
    rows = list(ws.iter_rows(values_only=True))
    for hi, row in enumerate(rows[:max_scan]):
        if row and all(resolve_column(row, COLS[k]) is not None for k in required):
            body = [r for r in rows[hi + 1:] if any(not is_empty(c) for c in r)]
            return _Table(list(row), body)
    return None


def parse_etoro_date(val) -> datetime:
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable eToro date {val!r}")


def extract_ticker(instrument: str) -> str:
    """'Taiwan Semiconductor ... - ADR (TSM)' -> 'TSM', 'BTC/USD' -> 'BTC',
    'Buy NVDA/USD' -> 'NVDA', 'Apple' -> 'APPLE'."""
    s = str(instrument or "").strip()
    low = s.lower()
    for p in _SHORT_PREFIXES + _LONG_PREFIXES:
        if low.startswith(p):
            s = s[len(p):].strip()
            break
    if not s:
        return "UNKNOWN"
    m = re.search(r"\(([A-Za-z0-9.\-]{1,10})\)\s*$", s)
    if m:
        return m.group(1).upper()
    if "/" in s:
        left = s.split("/")[0].strip()
        if 1 <= len(left) <= 10 and left.replace(".", "").replace("-", "").isalnum():
            return left.upper()
    if len(s) <= 6 and s.replace(".", "").replace("-", "").isalnum():
        return s.upper()
    return s.split()[0].upper()


def _activity_kind(row_type: str) -> str:
    t = _norm(row_type)
    for kind, words in ACTIVITY_RULES:
        if any(w in t for w in words):
            return kind
    return ""


def _is_deposit_fx_fee(row_type: str) -> bool:
    """'Poplatek za směnu měny při vkladu' / 'Deposit conversion fee'."""
    t = _norm(row_type)
    return (_activity_kind(t) == "fee"
            and any(w in t for w in ("vklad", "deposit", "conversion", "směn", "smen")))


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def _sheet(wb, key):
    for name in wb.sheetnames:
        n = _norm(name)
        if any(w in n for w in SHEETS[key]):
            return wb[name]
    return None


def detect(filepath: str) -> bool:
    low = filepath.lower()
    if low.endswith(".xlsx"):
        try:
            wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        except Exception:
            return False
        try:
            names = [_norm(s) for s in wb.sheetnames]
        finally:
            wb.close()
        return any(w in n for n in names for w in DETECT_SHEETS)
    if low.endswith(".csv"):
        try:
            with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
                sample = f.read(4096)
        except OSError:
            return False
        return (("Position ID" in sample and "Open Rate" in sample)
                or ("ID pozice" in sample and "Otevírací" in sample))
    return False


# --------------------------------------------------------------------------
# FX rate (USD -> EUR)
# --------------------------------------------------------------------------

_live_rate_cache = {}


def _live_usd_eur():
    if "rate" not in _live_rate_cache:
        rate = None
        try:
            resp = requests.get("https://api.frankfurter.app/latest?from=USD&to=EUR", timeout=5)
            if resp.status_code == 200:
                rate = resp.json().get("rates", {}).get("EUR")
        except requests.RequestException:
            rate = None
        _live_rate_cache["rate"] = float(rate) if rate else None
    return _live_rate_cache["rate"]


def _statement_rate(activity, closed) -> tuple:
    """(rate, source) for USD->EUR."""
    rates = []
    if activity:
        rows = [(parse_etoro_date(activity.get(r, "date")), r) for r in activity.rows
                if activity.get(r, "date") is not None]
        fx_fees = [(d, abs(safe_float(activity.get(r, "amount")))) for d, r in rows
                   if _is_deposit_fx_fee(activity.get(r, "row_type", ""))]
        for d, r in rows:
            if _activity_kind(activity.get(r, "row_type", "")) != "deposit":
                continue
            m = _EUR_IN_DETAILS.search(str(activity.get(r, "details", "")))
            usd = abs(safe_float(activity.get(r, "amount")))
            if m and usd > 0:
                fee = sum(f for fd, f in fx_fees if abs((fd - d).total_seconds()) <= 2)
                rates.append(safe_float(m.group(1)) / (usd + fee))
    if rates:
        return statistics.median(rates), "deposits"
    if closed:
        for r in closed.rows:
            usd, eur = safe_float(closed.get(r, "profit_usd")), safe_float(closed.get(r, "profit_eur"))
            if abs(usd) >= 1 and eur:
                rates.append(eur / usd)
    if rates:
        return statistics.median(rates), "closed-position profit"
    live = _live_usd_eur()
    if live:
        return live, "live ECB rate"
    return STATIC_USD_EUR, "static fallback"


# --------------------------------------------------------------------------
# Sheet parsers
# --------------------------------------------------------------------------

class _Ctx:
    def __init__(self, st: Statement, rate: float):
        self.st = st
        self.rate = rate
        self.unknown = {}

    def tx(self, **kw):
        kw.setdefault("source", BROKER)
        kw.setdefault("account", self.st.account)
        self.st.transactions.append(create_transaction(**kw))

    def uid(self, *parts):
        return ":".join(["etoro", str(self.st.account or "na"), *map(str, parts)])


def _parse_details(st: Statement, ws):
    for row in ws.iter_rows(max_row=20, values_only=True):
        if not row or is_empty(row[0]) or len(row) < 2 or is_empty(row[1]):
            continue
        label, value = _norm(row[0]), row[1]
        for key, words in DETAILS.items():
            if label in words:
                if key == "holder":
                    if _norm(value) not in _NOT_A_NAME:
                        st.holder = str(value).strip()
                elif key == "username":
                    st.account = str(value).strip()
                elif key == "created":
                    st.generated_at = parse_etoro_date(value)
                elif key == "start":
                    st.period_start = parse_etoro_date(value)
                elif key == "end":
                    st.period_end = parse_etoro_date(value)


def _parse_closed(ctx: _Ctx, closed: _Table) -> set:
    closed_ids = set()
    for r in closed.rows:
        instrument = str(closed.get(r, "action", "") or "")
        open_raw = closed.get(r, "open_date")
        if not instrument or open_raw is None:
            continue
        pid = norm_id(closed.get(r, "position_id"))
        units = safe_float(closed.get(r, "units"))
        amount = safe_float(closed.get(r, "amount"))
        open_rate = safe_float(closed.get(r, "open_rate"))
        leverage = safe_float(closed.get(r, "leverage"), 1.0) or 1.0
        if units <= 0 and open_rate > 0:
            units = amount * leverage / open_rate
        if units <= 0:
            continue
        if closed.get(r, "profit_eur") is not None:
            profit = safe_float(closed.get(r, "profit_eur"))
        elif closed.get(r, "profit_usd") is not None:
            profit = safe_float(closed.get(r, "profit_usd")) * ctx.rate
        else:
            profit = None
        open_date = parse_etoro_date(open_raw)
        close_raw = closed.get(r, "close_date")
        close_date = parse_etoro_date(close_raw) if close_raw is not None else open_date
        side = _norm(closed.get(r, "long_short", ""))
        short = side.startswith("short") or _norm(instrument).startswith(_SHORT_PREFIXES)

        buy_price = open_rate * ctx.rate if open_rate > 0 else amount * ctx.rate / units
        if profit is not None:
            sell_price = buy_price + profit / units
        else:
            sell_price = safe_float(closed.get(r, "close_rate")) * ctx.rate
        common = dict(ticker=extract_ticker(instrument), qty=units, position_id=pid,
                      isin=closed.get(r, "isin"), name=instrument)
        ctx.tx(date=close_date if short else open_date, tx_type="BUY", price=buy_price,
               uid=ctx.uid("pos", pid, "buy") if pid else None, **common)
        ctx.tx(date=close_date, tx_type="SELL", price=max(sell_price, 0.0), realized_pnl=profit,
               uid=ctx.uid("pos", pid, "sell") if pid else None, **common)
        if pid:
            closed_ids.add(pid)
    return closed_ids


def _cash_ticker(details: str) -> str:
    return extract_ticker(details) if details and "/" in details else "CASH"


def _parse_activity(ctx: _Ctx, activity: _Table, closed_ids: set, use_dividends: bool):
    for r in activity.rows:
        date_raw = activity.get(r, "date")
        if date_raw is None:
            continue
        when = parse_etoro_date(date_raw)
        row_type = str(activity.get(r, "row_type", "") or "")
        kind = _activity_kind(row_type)
        usd = safe_float(activity.get(r, "amount"))
        details = str(activity.get(r, "details", "") or "").strip()
        pid = norm_id(activity.get(r, "position_id"))
        eur = abs(usd) * ctx.rate

        if kind == "skip" or (kind == "dividend" and not use_dividends):
            continue
        if kind == "open":
            units = safe_float(activity.get(r, "units"))
            if pid and pid not in closed_ids and units > 0:
                ctx.tx(date=when, ticker=extract_ticker(details), tx_type="BUY", qty=units,
                       price=eur / units, position_id=pid, uid=ctx.uid("pos", pid, "buy"))
            continue
        if kind == "deposit":
            m = _EUR_IN_DETAILS.search(details)
            ctx.tx(date=when, ticker="CASH", tx_type="DEPOSIT",
                   qty=safe_float(m.group(1)) if m else eur, price=1.0)
        elif kind == "withdrawal":
            m = _EUR_IN_DETAILS.search(details)
            ctx.tx(date=when, ticker="CASH", tx_type="WITHDRAWAL",
                   qty=safe_float(m.group(1)) if m else eur, price=1.0)
        elif kind in ("fee", "by_sign", "interest") and usd != 0:
            income = kind == "interest" or (kind == "by_sign" and usd > 0)
            ctx.tx(date=when, ticker=_cash_ticker(details) if not income else "CASH",
                   tx_type="INTEREST" if income else "FEE", qty=eur, price=1.0, position_id=pid)
        elif kind == "dividend" and usd != 0:
            ctx.tx(date=when, ticker=_cash_ticker(details), tx_type="DIVIDEND", qty=eur,
                   price=1.0, position_id=pid)
        elif not kind:
            ctx.unknown[row_type] = ctx.unknown.get(row_type, 0) + 1


def _parse_dividends(ctx: _Ctx, div: _Table) -> int:
    booked = 0
    for r in div.rows:
        date_raw = div.get(r, "div_date")
        if date_raw is None:
            continue
        when = parse_etoro_date(date_raw)
        name = str(div.get(r, "div_name", "") or "")
        ticker = extract_ticker(name)
        net = (safe_float(div.get(r, "div_net_eur")) if div.get(r, "div_net_eur") is not None
               else safe_float(div.get(r, "div_net_usd")) * ctx.rate)
        wht = (safe_float(div.get(r, "div_wht_eur")) if div.get(r, "div_wht_eur") is not None
               else safe_float(div.get(r, "div_wht_usd")) * ctx.rate)
        net, wht = abs(net), abs(wht)
        pid = norm_id(div.get(r, "position_id"))
        stamp = when.isoformat()
        common = dict(ticker=ticker, price=1.0, position_id=pid, isin=div.get(r, "isin"), name=name)
        if net + wht > 0:
            ctx.tx(date=when, tx_type="DIVIDEND", qty=net + wht,
                   uid=ctx.uid("div", pid, stamp) if pid else None, **common)
            booked += 1
        if wht > 0:
            ctx.tx(date=when, tx_type="WITHHOLDING_TAX", qty=wht,
                   uid=ctx.uid("div", pid, stamp, "wht") if pid else None, **common)
    return booked


def _parse_xlsx(filepath: str, st: Statement):
    wb = openpyxl.load_workbook(filepath, data_only=True)
    try:
        details = _sheet(wb, "details")
        if details is not None:
            _parse_details(st, details)
        closed_ws, act_ws, div_ws = _sheet(wb, "closed"), _sheet(wb, "activity"), _sheet(wb, "dividends")
        closed = _table(closed_ws, ["action", "open_date"]) if closed_ws is not None else None
        activity = _table(act_ws, ["date", "row_type", "amount"]) if act_ws is not None else None
        div = _table(div_ws, ["div_date", "div_name"]) if div_ws is not None else None
    finally:
        wb.close()

    rate, rate_src = _statement_rate(activity, closed)
    if rate_src == "static fallback":
        st.warnings.append(f"USD->EUR rate unavailable; using static {STATIC_USD_EUR}.")
    ctx = _Ctx(st, rate)
    closed_ids = _parse_closed(ctx, closed) if closed else set()
    dividend_rows = _parse_dividends(ctx, div) if div else 0
    if activity:
        _parse_activity(ctx, activity, closed_ids, use_dividends=dividend_rows == 0)
    if ctx.unknown:
        listed = ", ".join(f"{k} ×{n}" for k, n in sorted(ctx.unknown.items()))
        st.warnings.append(f"Skipped {sum(ctx.unknown.values())} activity row(s) of unknown type: {listed}.")


# --------------------------------------------------------------------------
# Legacy CSV (Portfolio -> History export)
# --------------------------------------------------------------------------

def _parse_csv(filepath: str, st: Statement):
    rate = _live_usd_eur()
    if not rate:
        rate = STATIC_USD_EUR
        st.warnings.append(f"USD->EUR rate unavailable; using static {STATIC_USD_EUR}.")
    with open(filepath, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()
    start = next((i for i, line in enumerate(lines[:10])
                  if "Position ID" in line or "ID pozice" in line), 0)
    reader = csv.reader(io.StringIO("".join(lines[start:])))
    header = next(reader, [])
    table = _Table(header, [r for r in reader if any(c.strip() for c in r)])
    ctx = _Ctx(st, rate)
    _parse_closed(ctx, table)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse(filepath: str) -> Statement:
    st = Statement(broker=BROKER, file=os.path.basename(filepath))
    if filepath.lower().endswith(".csv"):
        _parse_csv(filepath, st)
    else:
        _parse_xlsx(filepath, st)
    if st.transactions:
        dates = [t["date"] for t in st.transactions]
        st.period_start = st.period_start or min(dates)
        st.period_end = st.period_end or max(dates)
    st.generated_at = st.generated_at or st.period_end
    return st
