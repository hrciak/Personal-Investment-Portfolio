"""
Symbol resolution
=================

Turns a broker's instrument identifier into the symbol the price feed uses.
No per-stock hardcoding: resolution is driven by data already in the statements.

  * user override (symbol_overrides.json)  -> whatever the user mapped
  * crypto                                 -> Yahoo "<TICKER>-EUR" pair
  * ISIN (Trading 212, eToro)              -> Yahoo Finance search API (e.g. VUSA.L)
  * XTB exchange suffix                    -> Yahoo suffix via a small generic table
                                              (.UK -> .L, .DE -> .DE, .FR -> .PA, ...)
  * everything else                        -> the plain ticker

Resolved symbols and their quote currencies are cached on disk
(symbol_cache.json, gitignored) so each lookup happens only once.

Overrides
---------
Create symbol_overrides.json in the project root to fix a symbol the automatic
resolution gets wrong. Keys are "TICKER" or "Broker:TICKER" (the latter wins);
values are a Yahoo symbol, or null to disable the price feed for that holding
(it is then valued at its last trade price):

    {"RHM": "RHM.DE", "Bitpanda:BEST": null}
"""

import os
import json
import threading

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_PATH = os.path.join(_ROOT, "symbol_cache.json")
OVERRIDES_PATH = os.path.join(_ROOT, "symbol_overrides.json")

_lock = threading.Lock()
_cache = None
_overrides = None
_NO_FEED = object()  # sentinel: override explicitly disabled the feed

# XTB appends an exchange code to the symbol (VUSA.UK, RHM.DE). Map it to the
# suffix Yahoo Finance uses. US listings need no suffix.
EXCHANGE_SUFFIX_MAP = {
    "US": "", "USA": "", "UK": ".L", "GB": ".L", "DE": ".DE", "FR": ".PA",
    "NL": ".AS", "ES": ".MC", "IT": ".MI", "CH": ".SW", "BE": ".BR",
    "PT": ".LS", "AT": ".VI", "IE": ".IR", "FI": ".HE", "SE": ".ST",
    "DK": ".CO", "NO": ".OL", "PL": ".WA",
}

# Quote currency implied by a Yahoo suffix — only a fallback for when the
# currency lookup fails. London (.L) is ambiguous (GBP vs GBp) and is left to
# the lookup / the engine's unit check.
SUFFIX_CURRENCY = {
    "": "USD", ".DE": "EUR", ".F": "EUR", ".PA": "EUR", ".AS": "EUR", ".MC": "EUR",
    ".MI": "EUR", ".BR": "EUR", ".LS": "EUR", ".VI": "EUR", ".IR": "EUR",
    ".HE": "EUR", ".SW": "CHF", ".ST": "SEK", ".CO": "DKK", ".OL": "NOK",
    ".WA": "PLN", ".TO": "CAD", ".L": "GBp",
}

_YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Preferred Yahoo exchange codes per ISIN home country, so an ISIN resolves to
# its primary listing rather than a thin secondary one. Falls back to any
# major exchange, then the first equity result.
_COUNTRY_EXCHANGES = {
    "US": {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "NYS"},
    "GB": {"LSE", "IOB"}, "IE": {"LSE", "ISE", "AMS", "GER"},
    "DE": {"GER", "FRA"}, "FR": {"PAR"}, "NL": {"AMS"}, "ES": {"MCE"},
    "IT": {"MIL"}, "CH": {"EBS", "VTX"}, "BE": {"BRU"}, "PT": {"LIS"},
    "AT": {"VIE"}, "FI": {"HEL"}, "SE": {"STO"}, "DK": {"CPH"}, "NO": {"OSL"},
}
_MAJOR_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "LSE", "GER",
                    "FRA", "PAR", "AMS", "MIL", "MCE", "EBS", "VTX"}


# --------------------------------------------------------------------------
# Disk cache / overrides
# --------------------------------------------------------------------------

def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2, sort_keys=True)
    except OSError as e:
        print(f"symbols: could not write {_CACHE_PATH}: {e}")


def cache_get(key: str, default=None):
    with _lock:
        return _load_cache().get(key, default)


def cache_has(key: str) -> bool:
    with _lock:
        return key in _load_cache()


def cache_set(key: str, value) -> None:
    with _lock:
        _load_cache()[key] = value
        _save_cache()


def load_overrides() -> dict:
    """Read symbol_overrides.json (re-read on every pipeline run via reset)."""
    global _overrides
    if _overrides is None:
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _overrides = {str(k).strip().upper(): v for k, v in raw.items()}
        except FileNotFoundError:
            _overrides = {}
        except (OSError, ValueError) as e:
            print(f"symbols: ignoring unreadable {OVERRIDES_PATH}: {e}")
            _overrides = {}
    return _overrides


def reset() -> None:
    """Forget in-memory state so edits to the JSON files are picked up."""
    global _cache, _overrides
    with _lock:
        _cache = None
        _overrides = None


def _override_for(ticker: str, source: str):
    ov = load_overrides()
    for key in (f"{source}:{ticker}".upper(), ticker.upper()):
        if key in ov:
            return _NO_FEED if ov[key] in (None, "") else str(ov[key]).strip()
    return None


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

def _yahoo_symbol_for_isin(isin: str):
    """Query Yahoo's search endpoint for an ISIN and return the best equity
    symbol, or None if not found. Prefers the ISIN's home-country listing.
    Raises on network errors so the caller can avoid caching a transient miss."""
    r = requests.get(_YAHOO_SEARCH, params={"q": isin, "quotesCount": 8, "newsCount": 0},
                     headers=_HEADERS, timeout=10)
    if r.status_code != 200:
        raise requests.HTTPError(f"Yahoo search HTTP {r.status_code}")
    quotes = [q for q in r.json().get("quotes", []) if q.get("symbol")]
    if not quotes:
        return None
    equities = [q for q in quotes if q.get("quoteType") in ("EQUITY", "ETF")] or quotes

    home = _COUNTRY_EXCHANGES.get(isin[:2].upper(), set())
    for pref in (home, _MAJOR_EXCHANGES):
        for q in equities:
            if q.get("exchange") in pref:
                return q["symbol"]
    return equities[0]["symbol"]


def _suffix_from_raw(raw_symbol: str):
    """Translate an XTB-style 'SYMBOL.EXCHANGE' into a Yahoo symbol, or None.
    Only the LAST dot is the exchange code ('BRK.B.US' -> base 'BRK.B')."""
    if not raw_symbol or "." not in raw_symbol:
        return None
    base, _, code = raw_symbol.strip().rpartition(".")
    code = code.upper()
    if code not in EXCHANGE_SUFFIX_MAP or not base:
        return None
    suffix = EXCHANGE_SUFFIX_MAP[code]
    base = base.upper()
    if suffix == "":
        base = base.replace(".", "-")  # Yahoo uses BRK-B for US class shares
    return base + suffix


def resolve_symbol(ticker: str, isin: str = None, raw_symbol: str = None,
                   source: str = "", asset_class: str = "Stock/ETF"):
    """Return the Yahoo symbol for a holding, or None when the user disabled
    its feed. Falls back to the plain ticker."""
    override = _override_for(ticker, source)
    if override is _NO_FEED:
        return None
    if override:
        return override

    if asset_class == "Crypto":
        return f"{ticker.upper()}-EUR"

    if isin:
        # US ISINs: Yahoo's ISIN search returns thin foreign cross-listings, but
        # the broker's ticker IS the US symbol (BRK.A -> BRK-A).
        if isin[:2].upper() == "US":
            return ticker.upper().replace(".", "-")
        key = f"isin:{isin}"
        if cache_has(key):
            cached = cache_get(key)
            if cached:
                return cached
        else:
            try:
                sym = _yahoo_symbol_for_isin(isin)
            except (requests.RequestException, ValueError):
                sym = False  # transient failure — do not cache, retry next load
            if sym is not False:
                cache_set(key, sym)  # cache definitive hits and misses
                if sym:
                    return sym

    if raw_symbol:
        sym = _suffix_from_raw(raw_symbol)
        if sym:
            return sym

    return ticker.upper()


def build_symbol_map(instruments: list[dict]) -> dict:
    """{key: yahoo_symbol_or_None} for instruments given as dicts with keys
    key, ticker, source, asset_class, isin, raw_symbol."""
    return {
        inst["key"]: resolve_symbol(inst["ticker"], inst.get("isin"), inst.get("raw_symbol"),
                                    inst.get("source", ""), inst.get("asset_class", "Stock/ETF"))
        for inst in instruments
    }


def guess_currency(symbol: str) -> str:
    """Best-effort quote currency from the Yahoo suffix (fallback only)."""
    if symbol.endswith("-EUR"):
        return "EUR"
    if symbol.endswith("-USD"):
        return "USD"
    dot = symbol.rfind(".")
    suffix = symbol[dot:].upper() if dot > 0 else ""
    return SUFFIX_CURRENCY.get(suffix, "USD")
