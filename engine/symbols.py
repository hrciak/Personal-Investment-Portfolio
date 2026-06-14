"""
Symbol resolution
=================

Turns a broker's instrument identifier into the symbol the price feeds use.
No per-stock hardcoding: resolution is driven by data already in the statements.

  * ISIN (Trading 212, eToro)  -> Yahoo Finance search API -> e.g. VUSA.L
  * XTB exchange suffix         -> Yahoo suffix via a small generic table
                                   (.UK -> .L, .DE -> .DE, .FR -> .PA, ...)
  * everything else             -> the plain ticker

Resolved symbols are cached to disk (symbol_cache.json) so each ISIN / symbol
is only looked up once.
"""

import os
import json
import requests

_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "symbol_cache.json")
_cache = None

# XTB appends an exchange code to the symbol (VUSA.UK, RHM.DE). Map it to the
# suffix Yahoo Finance uses. US listings need no suffix. This is ~a dozen
# generic exchange codes — not a per-instrument list.
EXCHANGE_SUFFIX_MAP = {
    "US": "", "USA": "", "UK": ".L", "GB": ".L", "DE": ".DE", "FR": ".PA",
    "NL": ".AS", "ES": ".MC", "IT": ".MI", "CH": ".SW", "BE": ".BR",
    "PT": ".LS", "AT": ".VI", "IE": ".IR", "FI": ".HE", "SE": ".ST",
    "DK": ".CO", "NO": ".OL", "PL": ".WA",
}

_YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Preferred Yahoo exchange codes per ISIN home country, so an ISIN resolves to
# its primary listing rather than a thin secondary one (e.g. a US name on the
# Mexican or Stuttgart exchange). Falls back to any major exchange, then first.
_COUNTRY_EXCHANGES = {
    "US": {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "NYS"},
    "GB": {"LSE", "IOB"}, "IE": {"LSE", "ISE", "AMS", "GER"},
    "DE": {"GER", "FRA"}, "FR": {"PAR"}, "NL": {"AMS"}, "ES": {"MCE"},
    "IT": {"MIL"}, "CH": {"EBS", "VTX"}, "BE": {"BRU"}, "PT": {"LIS"},
    "AT": {"VIE"}, "FI": {"HEL"}, "SE": {"STO"}, "DK": {"CPH"}, "NO": {"OSL"},
}
_MAJOR_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "LSE", "GER",
                    "FRA", "PAR", "AMS", "MIL", "MCE", "EBS", "VTX"}


def _load_cache():
    global _cache
    if _cache is None:
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def _save_cache():
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2, sort_keys=True)
    except Exception:
        pass


def _yahoo_symbol_for_isin(isin: str):
    """Query Yahoo's search endpoint for an ISIN and return the best equity
    symbol, or None if not found. Prefers the ISIN's home-country listing.
    Raises on network errors so the caller can avoid caching a transient miss."""
    r = requests.get(_YAHOO_SEARCH, params={"q": isin, "quotesCount": 8, "newsCount": 0},
                     headers=_HEADERS, timeout=10)
    if r.status_code != 200:
        return None
    quotes = [q for q in r.json().get("quotes", []) if q.get("symbol")]
    if not quotes:
        return None
    equities = [q for q in quotes if q.get("quoteType") in ("EQUITY", "ETF")] or quotes

    country = isin[:2].upper()
    home = _COUNTRY_EXCHANGES.get(country, set())
    # 1) home-country listing, 2) any major exchange, 3) first equity
    for pref in (home, _MAJOR_EXCHANGES):
        for q in equities:
            if q.get("exchange") in pref:
                return q["symbol"]
    return equities[0]["symbol"]


def _suffix_from_raw(raw_symbol: str):
    """Translate an XTB-style 'SYMBOL.EXCHANGE' into a Yahoo symbol, or None."""
    if not raw_symbol or "." not in raw_symbol:
        return None
    base, _, code = raw_symbol.rpartition(".")
    code = code.strip().upper()
    if code in EXCHANGE_SUFFIX_MAP:
        return base.strip().upper() + EXCHANGE_SUFFIX_MAP[code]
    return None


def resolve_symbol(ticker: str, isin: str = None, raw_symbol: str = None) -> str:
    """Return the Yahoo symbol for a holding. Falls back to the plain ticker.
    Results are cached on disk keyed by ISIN (preferred) or raw symbol."""
    cache = _load_cache()

    if isin:
        # US ISINs: Yahoo's ISIN search returns thin foreign cross-listings, but
        # the broker's ticker IS the US symbol. Use it, normalizing class-share
        # dots to Yahoo's dashes (BRK.A -> BRK-A). No exchange suffix needed.
        if isin[:2].upper() == "US":
            return ticker.replace(".", "-")

        key = f"isin:{isin}"
        if key in cache:
            return cache[key] or ticker
        try:
            sym = _yahoo_symbol_for_isin(isin)
        except Exception:
            return ticker  # network error — don't cache, retry on a later load
        cache[key] = sym  # cache definitive hits and misses (200-but-not-found)
        _save_cache()
        if sym:
            return sym

    if raw_symbol:
        key = f"sym:{raw_symbol.upper()}"
        if key in cache:
            return cache[key] or ticker
        sym = _suffix_from_raw(raw_symbol)
        if sym:
            cache[key] = sym
            _save_cache()
            return sym

    return ticker


def build_symbol_map(transactions: list) -> dict:
    """Build {ticker: yahoo_symbol} for every non-crypto holding, using the
    best identifier seen for that ticker across all transactions."""
    # Gather the richest identifier per ticker (ISIN preferred, then raw symbol)
    by_ticker = {}
    for t in transactions:
        tk = t.get("ticker")
        if not tk or tk == "CASH":
            continue
        info = by_ticker.setdefault(tk, {"isin": None, "raw_symbol": None})
        if t.get("isin") and not info["isin"]:
            info["isin"] = t["isin"]
        if t.get("raw_symbol") and not info["raw_symbol"]:
            info["raw_symbol"] = t["raw_symbol"]

    symbol_map = {}
    for tk, info in by_ticker.items():
        symbol_map[tk] = resolve_symbol(tk, info["isin"], info["raw_symbol"])
    return symbol_map
