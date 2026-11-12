"""
Market data (Yahoo Finance, CoinGecko fallback)
===============================================

Everything this module hands to the engine is **EUR per unit**, as a daily
pandas Series indexed by naive, normalized dates. That single convention is
what lets holdings from different brokers, exchanges and currencies be summed.

  * Stocks/ETFs: Yahoo daily close (split-adjusted, NOT dividend-adjusted —
    dividends are booked separately as income, so dividend-adjusted prices
    would count them twice). Converted from the listing's quote currency
    (looked up once per symbol and cached, minor units such as GBp handled)
    to EUR with Yahoo FX history.
  * Crypto: Yahoo "<TICKER>-EUR", then "<TICKER>-USD" converted to EUR, then
    CoinGecko for coins with an explicit id below.
  * Benchmark: S&P 500 total return (dividends reinvested) in EUR.

The engine is written against the small MarketData interface implemented by
YahooMarketData, so tests (and offline use) can inject their own provider.
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd
import requests
import yfinance as yf

from engine import symbols

# Explicit CoinGecko ids. Only coins listed here use the CoinGecko fallback —
# guessing an id from the ticker silently prices the wrong coin.
COINGECKO_ID_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "ADA": "cardano",
    "XRP": "ripple", "DOT": "polkadot", "MATIC": "matic-network", "POL": "polygon-ecosystem-token",
    "BNB": "binancecoin", "AVAX": "avalanche-2", "LINK": "chainlink",
    "LTC": "litecoin", "DOGE": "dogecoin", "ATOM": "cosmos", "ALGO": "algorand",
    "UNI": "uniswap", "AAVE": "aave", "NEAR": "near", "FTM": "fantom",
    "HBAR": "hedera-hashgraph", "ICP": "internet-computer", "APE": "apecoin",
    "EGLD": "elrond-erd-2", "AUDIO": "audius",
    "SCRT": "secret", "OSMO": "osmosis", "LUNC": "terra-luna", "USDT": "tether",
    "USDC": "usd-coin", "DAI": "dai",
}

# Yahoo quotes some listings in a currency's minor unit (pence, cents).
MINOR_UNITS = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0),
               "ILA": ("ILS", 100.0)}

BENCHMARKS = [
    ("^SP500TR", "S&P 500 Total Return"),
    ("SPY", "S&P 500 (SPY, dividends reinvested)"),
    ("^GSPC", "S&P 500 (price index)"),
]

_cache: dict = {}


def clear_cache() -> None:
    """Drop downloaded series and reload the symbol cache/overrides from disk."""
    _cache.clear()
    symbols.reset()


@dataclass
class PriceSeries:
    """EUR price history for one holding."""
    series: pd.Series                    # EUR per unit, daily (trading days only), may be empty
    symbol: str | None = None
    currency: str | None = None
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Low-level download helpers
# --------------------------------------------------------------------------

def _normalize_index(s: pd.Series) -> pd.Series:
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s = s.copy()
    s.index = idx.normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.astype(float)


def _download(symbol_list, start: dt.date, end: dt.date, adjusted: bool = False) -> dict:
    """Batched daily closes {symbol: Series}. `end` is inclusive."""
    syms = sorted({s for s in symbol_list if s})
    if not syms:
        return {}
    key = ("dl", tuple(syms), start, end, adjusted)
    if key in _cache:
        return _cache[key]

    out = {}
    try:
        df = yf.download(syms, start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(),
                         auto_adjust=adjusted, progress=False, threads=True)
    except Exception as e:  # network / Yahoo errors must not kill the pipeline
        print(f"prices: Yahoo download failed ({e})")
        df = None
    if df is not None and not df.empty:
        close = df["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(syms[0])
        for sym in syms:
            if sym in close.columns:
                s = close[sym].dropna()
                if not s.empty:
                    out[sym] = _normalize_index(s)
    _cache[key] = out
    return out


def quote_currency(symbol: str) -> str:
    """Quote currency of a Yahoo symbol (e.g. 'USD', 'GBp'), cached on disk."""
    key = f"ccy:{symbol}"
    if symbols.cache_has(key):
        return symbols.cache_get(key)
    ccy = None
    try:
        ccy = yf.Ticker(symbol).fast_info["currency"]
    except Exception:  # yfinance raises a variety of errors for unknown symbols
        ccy = None
    if ccy:
        symbols.cache_set(key, ccy)
        return ccy
    return symbols.guess_currency(symbol)


def _quote_currencies(symbol_list) -> dict:
    syms = sorted(set(symbol_list))
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(syms, pool.map(quote_currency, syms)))


def eur_per_unit(currencies, start: dt.date, end: dt.date) -> dict:
    """{currency: daily Series of EUR per 1 unit of that currency}, calendar
    days, forward/back-filled. Minor units (GBp) are already divided out."""
    majors = {MINOR_UNITS.get(c, (c, 1.0))[0] for c in currencies if c} - {"EUR"}
    fx_syms = {f"EUR{m}=X": m for m in majors}
    closes = _download(list(fx_syms), start - dt.timedelta(days=10), end)
    days = pd.date_range(start - dt.timedelta(days=10), end, freq="D")

    per_major = {"EUR": pd.Series(1.0, index=days)}
    for sym, major in fx_syms.items():
        s = closes.get(sym)
        if s is not None and not s.empty:
            per_major[major] = (1.0 / s).reindex(days).ffill().bfill()

    out = {}
    for c in currencies:
        if not c:
            continue
        major, factor = MINOR_UNITS.get(c, (c, 1.0))
        if major in per_major:
            out[c] = per_major[major] / factor
    return out


def _to_eur(series: pd.Series, currency: str, fx: dict):
    """Convert a local-currency series to EUR, or None if FX is unavailable."""
    if currency == "EUR":
        return series
    rate = fx.get(currency)
    if rate is None:
        return None
    return (series * rate.reindex(series.index).ffill().bfill()).dropna()


def _coingecko_history(coin_id: str, start: dt.date, end: dt.date) -> pd.Series:
    """Daily EUR history from CoinGecko (free tier: last 365 days only)."""
    days = max(1, min((end - start).days + 1, 365))
    try:
        resp = requests.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                            params={"vs_currency": "eur", "days": days}, timeout=10)
        resp.raise_for_status()
        prices = resp.json().get("prices") or []
    except (requests.RequestException, ValueError) as e:
        print(f"prices: CoinGecko history failed for {coin_id} ({e})")
        return pd.Series(dtype=float)
    if not prices:
        return pd.Series(dtype=float)
    df = pd.DataFrame(prices, columns=["ts", "price"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.normalize()
    return df.groupby("date")["price"].last().astype(float)


# --------------------------------------------------------------------------
# Public provider
# --------------------------------------------------------------------------

class YahooMarketData:
    """Default MarketData implementation.

    daily_prices_eur(instruments, start, end) -> {key: PriceSeries}
        instruments: dicts with key, ticker, source, asset_class, isin, raw_symbol
    benchmark_eur(start, end) -> (Series of EUR values or None, label)
    """

    def daily_prices_eur(self, instruments: list, start: dt.date, end: dt.date) -> dict:
        result = {}
        crypto, stocks = [], []
        for inst in instruments:
            if inst["asset_class"] == "Cash":
                result[inst["key"]] = PriceSeries(pd.Series(dtype=float), None, "EUR", ["cash"])
                continue
            sym = symbols.resolve_symbol(inst["ticker"], inst.get("isin"), inst.get("raw_symbol"),
                                         inst.get("source", ""), inst["asset_class"])
            if sym is None:
                result[inst["key"]] = PriceSeries(pd.Series(dtype=float), None, None,
                                                  ["price feed disabled in symbol_overrides.json"])
                continue
            (crypto if inst["asset_class"] == "Crypto" else stocks).append((inst, sym))

        fetch_start = start - dt.timedelta(days=10)  # lets the first day forward-fill
        result.update(self._stocks(stocks, fetch_start, end))
        result.update(self._crypto(crypto, fetch_start, end))
        return result

    def _stocks(self, items, start, end) -> dict:
        out = {}
        if not items:
            return out
        closes = _download([s for _, s in items], start, end)
        missing = [s for _, s in items if s not in closes]
        if missing:  # one retry: Yahoo batch downloads drop symbols sporadically
            closes.update(_download(missing, start, end))
        ccys = _quote_currencies([s for _, s in items if s in closes])
        fx = eur_per_unit(set(ccys.values()), start, end)
        for inst, sym in items:
            raw = closes.get(sym)
            if raw is None:
                out[inst["key"]] = PriceSeries(pd.Series(dtype=float), sym, None,
                                               [f"no Yahoo data for {sym}"])
                continue
            ccy = ccys.get(sym) or symbols.guess_currency(sym)
            eur = _to_eur(raw, ccy, fx)
            if eur is None:
                out[inst["key"]] = PriceSeries(pd.Series(dtype=float), sym, ccy,
                                               [f"no FX rate for {ccy}"])
            else:
                out[inst["key"]] = PriceSeries(eur, sym, ccy)
        return out

    def _crypto(self, items, start, end) -> dict:
        out = {}
        if not items:
            return out
        closes = _download([s for _, s in items], start, end)
        usd_alt = {}
        for inst, sym in items:
            if sym not in closes and sym.endswith("-EUR"):
                usd_alt[sym] = sym[:-4] + "-USD"
        usd_closes = _download(list(usd_alt.values()), start, end) if usd_alt else {}
        fx = eur_per_unit({"USD"}, start, end) if usd_closes else {}

        for inst, sym in items:
            key = inst["key"]
            if sym in closes:
                out[key] = PriceSeries(closes[sym], sym, "EUR")
                continue
            alt = usd_alt.get(sym)
            if alt and alt in usd_closes:
                eur = _to_eur(usd_closes[alt], "USD", fx)
                if eur is not None and not eur.empty:
                    out[key] = PriceSeries(eur, alt, "USD", [f"{sym} unavailable, used {alt}"])
                    continue
            coin_id = COINGECKO_ID_MAP.get(inst["ticker"].upper())
            if coin_id:
                s = _coingecko_history(coin_id, start, end)
                if not s.empty:
                    out[key] = PriceSeries(s, f"coingecko:{coin_id}", "EUR",
                                           ["CoinGecko (history limited to 365 days)"])
                    continue
            out[key] = PriceSeries(pd.Series(dtype=float), sym, None,
                                   [f"no price data for {sym}"])
        return out

    def benchmark_eur(self, start: dt.date, end: dt.date):
        fetch_start = start - dt.timedelta(days=10)
        fx = None
        for sym, label in BENCHMARKS:
            s = _download([sym], fetch_start, end, adjusted=True).get(sym)
            if s is None or s.empty:
                continue
            if fx is None:
                fx = eur_per_unit({"USD"}, fetch_start, end)
            eur = _to_eur(s, "USD", fx)
            if eur is not None and not eur.empty:
                return eur, f"{label} in EUR"
        print("prices: all benchmark symbols failed; S&P 500 line will be empty.")
        return None, "S&P 500 (unavailable)"
