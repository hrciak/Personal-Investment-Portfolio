"""
Shared transaction model for all broker parsers
===============================================

Every parser turns a broker export into a list of transaction dicts built by
`create_transaction`. The engine consumes exactly these dicts, so the rules
below are the contract between ingestion and analytics:

  * date      naive datetime in UTC (tz-aware input is converted, then the
              tzinfo is dropped). Unparseable dates raise ValueError.
  * type      one of TX_TYPES (see the table below).
  * qty       non-negative units (trades, in-kind flows) or the EUR amount for
              cash rows (with price = 1.0).
  * price     non-negative EUR per unit.
  * fee       non-negative EUR, NOT included in qty * price.
  * realized_pnl  broker-reported EUR P/L of a SELL, net of that trade's fees
              where the broker allows it; None lets the engine compute it.
  * uid       stable broker-unique id used to dedupe overlapping exports.
  * snapshot  True only for BUY legs synthesized from an open-positions
              snapshot (the loader keeps those only from the newest snapshot).

Transaction types and how the engine books them:

  DEPOSIT / WITHDRAWAL   external cash flow in / out
  BUY                    cash -= qty*price + fee, position += qty
  SELL                   cash += qty*price - fee, position -= qty
  DIVIDEND               gross cash dividend (ticker = holding or "CASH")
  WITHHOLDING_TAX        tax withheld (ticker = holding, or "CASH")
  INTEREST               cash income not tied to a holding (interest,
                         cashback, promotional gifts, refunds)
  FEE                    non-trade cash cost (SEC fee, FX fee, commissions)
  REWARD                 in-kind income (staking): position += qty at cost
                         qty*price (fair value, may be 0), no cash
  TRANSFER_IN / _OUT     in-kind external flow of units at fair value, no cash
"""

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

TX_TYPES = frozenset({
    "DEPOSIT", "WITHDRAWAL", "BUY", "SELL", "DIVIDEND", "WITHHOLDING_TAX",
    "INTEREST", "FEE", "REWARD", "TRANSFER_IN", "TRANSFER_OUT",
})

BROKERS = ("XTB", "Bitpanda", "eToro", "Trading212")

# Fiat currencies are cash. Stablecoins are NOT: they track USD (not EUR) and
# carry issuer risk, so they are priced like any other crypto asset.
FIAT_TICKERS = {"CASH", "EUR", "USD", "GBP", "CHF"}
STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "EURC", "EURT", "TUSD", "USDP",
               "PYUSD", "FDUSD"}
CRYPTO_TICKERS = {
    "BTC", "ETH", "SOL", "ADA", "DOT", "MATIC", "BNB", "XRP", "AVAX", "LINK",
    "LTC", "DOGE", "ATOM", "ALGO", "UNI", "AAVE", "NEAR", "FTM", "HBAR", "ICP",
    "APE", "EGLD",
} | STABLECOINS

# Cell values brokers use for "no value"
_EMPTY_MARKERS = {"", "-", "n/a", "nan", "none", "není k dispozici"}


def classify_asset(ticker: str, source: str = "", asset_class_str: str = "") -> str:
    """Classify a ticker as "Crypto", "Stock/ETF" or "Cash"."""
    if not ticker:
        return "Stock/ETF"
    t_up = ticker.upper()
    if t_up in FIAT_TICKERS:
        return "Cash"
    if t_up in CRYPTO_TICKERS:
        return "Crypto"
    if (asset_class_str or "").strip().lower() == "cryptocurrency":
        return "Crypto"
    return "Stock/ETF"


def is_empty(val) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    return str(val).strip().lower() in _EMPTY_MARKERS


def safe_float(val, default: float = 0.0) -> float:
    """Parse a broker number: tolerates None/'-', currency signs, thousands
    commas, '%' and accounting-style negatives '(1.23)'."""
    if is_empty(val):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(" ", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = re.sub(r"[€$£%]", "", s).replace(",", "")
    try:
        num = float(s)
    except ValueError:
        return default
    return -num if neg else num


def to_naive_utc(value) -> datetime:
    """Normalize a datetime-like value to a naive UTC datetime.

    Naive input is assumed to already be UTC (parsers localize broker-local
    times before calling this). Raises ValueError when the value is unusable.
    """
    if is_empty(value):
        raise ValueError(f"missing date: {value!r}")
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            raise ValueError("missing date: NaT")
        value = value.to_pydatetime()
    elif isinstance(value, datetime):
        pass
    elif isinstance(value, date):
        value = datetime(value.year, value.month, value.day)
    else:
        try:
            ts = pd.Timestamp(str(value).strip())
        except (ValueError, TypeError) as e:
            raise ValueError(f"unparseable date: {value!r}") from e
        if pd.isna(ts):
            raise ValueError(f"unparseable date: {value!r}")
        value = ts.to_pydatetime()
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def strip_exchange_suffix(symbol: str) -> str:
    """'VUSA.DE' -> 'VUSA', 'BRK.B.US' -> 'BRK.B'. Only a trailing 2-4 letter
    exchange code is removed, so share-class dots ('BRK.B') survive."""
    s = (symbol or "").strip()
    base, dot, suffix = s.rpartition(".")
    if dot and base and 2 <= len(suffix) <= 4 and suffix.isalpha():
        return base
    return s


def create_transaction(
    date,
    ticker: str,
    tx_type: str,
    qty: float,
    price: float,
    fee: float = 0.0,
    realized_pnl: float = None,
    position_id: str = None,
    market_price_at_export: float = None,
    source: str = "",
    currency: str = "EUR",
    isin: str = None,
    raw_symbol: str = None,
    asset_class_hint: str = "",
    uid: str = None,
    account: str = None,
    snapshot: bool = False,
    name: str = None,
) -> dict:
    tx_type = (tx_type or "").strip().upper()
    if tx_type not in TX_TYPES:
        raise ValueError(f"unknown transaction type {tx_type!r}")
    isin_clean = None if is_empty(isin) else str(isin).strip().upper()
    return {
        "date": to_naive_utc(date),
        "ticker": str(ticker).strip().upper() if not is_empty(ticker) else "UNKNOWN",
        "type": tx_type,
        "qty": abs(safe_float(qty)),
        "price": abs(safe_float(price)),
        "fee": abs(safe_float(fee)),
        "realized_pnl": None if realized_pnl is None else float(realized_pnl),
        "position_id": None if is_empty(position_id) else str(position_id),
        "market_price_at_export": (None if market_price_at_export is None
                                   else abs(float(market_price_at_export))),
        "source": source,
        "currency": (currency or "EUR").upper(),
        # Identifiers used to resolve the right Yahoo / CoinGecko symbol
        "isin": isin_clean,
        # Broker symbol WITH its exchange suffix (e.g. XTB "VUSA.DE")
        "raw_symbol": None if is_empty(raw_symbol) else str(raw_symbol).strip(),
        # Broker's own asset-class label (e.g. Bitpanda "Cryptocurrency")
        "asset_class_hint": asset_class_hint or "",
        "uid": None if is_empty(uid) else str(uid),
        "account": None if is_empty(account) else str(account),
        "snapshot": bool(snapshot),
        "name": None if is_empty(name) else str(name).strip(),
    }


@dataclass
class Statement:
    """One parsed broker file plus the metadata the loader needs to merge it
    with overlapping exports."""
    broker: str
    file: str = ""
    account: str = None
    holder: str = None
    period_start: datetime = None
    period_end: datetime = None
    snapshot_at: datetime = None
    generated_at: datetime = None
    transactions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # Cash balance the broker printed on the statement (sanity checks only)
    reported_cash: float = None


def norm_id(val) -> str:
    """Normalize a broker id cell: 1688996043 / 1688996043.0 / '1688996043'
    all become '1688996043'. Returns None for empty cells."""
    if is_empty(val):
        return None
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    s = str(val).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s
