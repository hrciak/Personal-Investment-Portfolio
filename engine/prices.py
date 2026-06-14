import yfinance as yf
import requests
import pandas as pd
from parsers.base import classify_asset

COINGECKO_ID_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "ADA": "cardano",
    "XRP": "ripple", "DOT": "polkadot", "MATIC": "matic-network", 
    "BNB": "binancecoin", "AVAX": "avalanche-2", "LINK": "chainlink", 
    "LTC": "litecoin", "DOGE": "dogecoin", "ATOM": "cosmos", "ALGO": "algorand",
    "UNI": "uniswap", "AAVE": "aave", "NEAR": "near", "FTM": "fantom", 
    "HBAR": "hedera-hashgraph", "ICP": "internet-computer", "APE": "apecoin", 
    "EGLD": "elrond-erd-2"
}

# Cache at module level
_current_prices_cache = {}
_historical_prices_cache = {}
_benchmark_cache = {}

def clear_cache():
    global _current_prices_cache, _historical_prices_cache, _benchmark_cache
    _current_prices_cache = {}
    _historical_prices_cache = {}
    _benchmark_cache = {}


def _yf_crypto_eur_current(tickers: list) -> dict:
    """Latest EUR price per crypto ticker via yfinance '<TICKER>-EUR' pairs,
    in one batched download. Returns {ticker: price} for coins that resolved."""
    out = {}
    if not tickers:
        return out
    syms = [f"{t}-EUR" for t in tickers]
    try:
        df = yf.download(syms, period="5d", progress=False, auto_adjust=True)
        if df is None or df.empty or "Close" not in df:
            return out
        close = df["Close"]
        if len(syms) == 1:
            s = (close.iloc[:, 0] if hasattr(close, "columns") else close).dropna()
            if not s.empty:
                out[tickers[0]] = float(s.iloc[-1])
        else:
            for t in tickers:
                sym = f"{t}-EUR"
                if sym in close.columns:
                    s = close[sym].dropna()
                    if not s.empty:
                        out[t] = float(s.iloc[-1])
    except Exception:
        pass
    return out

def get_current_prices(tickers: list[str], fallback_prices: dict = None, source_map: dict = None,
                       asset_class_map: dict = None, symbol_map: dict = None) -> dict[str, float]:
    result = {}
    if fallback_prices is None:
        fallback_prices = {}
    if source_map is None:
        source_map = {}
    if asset_class_map is None:
        asset_class_map = {}
    if symbol_map is None:
        symbol_map = {}

    to_fetch_stocks = set()
    to_fetch_crypto = set()

    for t in tickers:
        if t in _current_prices_cache:
            result[t] = _current_prices_cache[t]
            continue

        asset_type = classify_asset(t, source_map.get(t, ""), asset_class_map.get(t, ""))
        if asset_type == "Cash":
            result[t] = 1.0
            _current_prices_cache[t] = 1.0
        elif asset_type == "Crypto":
            to_fetch_crypto.add(t)
        else:
            to_fetch_stocks.add(t)

    # Fetch Crypto.
    # Primary: yfinance "<TICKER>-EUR" pairs in ONE batched call. This is robust
    # and does not share CoinGecko's free-tier rate limit (which the per-coin
    # history calls exhaust, previously forcing every coin to fall back to its
    # average cost). CoinGecko is the fallback for any coin yfinance lacks.
    if to_fetch_crypto:
        crypto_list = list(to_fetch_crypto)
        got = _yf_crypto_eur_current(crypto_list)

        missing = [t for t in crypto_list if t not in got]
        if missing:
            cg_ids = {t: COINGECKO_ID_MAP.get(t, t.lower()) for t in missing}
            try:
                resp = requests.get(
                    f"https://api.coingecko.com/api/v3/simple/price?ids={','.join(cg_ids.values())}&vs_currencies=eur",
                    timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for t in missing:
                        price = data.get(cg_ids[t], {}).get("eur")
                        if price is not None:
                            got[t] = float(price)
            except Exception:
                pass

        for t in crypto_list:
            result[t] = got.get(t, fallback_prices.get(t, 0.0))
            _current_prices_cache[t] = result[t]

    # Fetch Stocks, using the resolved Yahoo symbol (e.g. VUSA -> VUSA.L) while
    # keeping results keyed by the original ticker. Missing prices fall back to
    # cost basis so a holding never shows as a total loss.
    if to_fetch_stocks:
        stock_list = list(to_fetch_stocks)
        sym_for = {t: (symbol_map.get(t) or t) for t in stock_list}
        dl_syms = sorted({s for s in sym_for.values() if s})
        prices_by_sym = {}
        try:
            df = yf.download(dl_syms, period="5d", progress=False, auto_adjust=True)
            if df is not None and not df.empty and "Close" in df:
                close = df["Close"]
                if hasattr(close, "columns"):
                    for sym in dl_syms:
                        if sym in close.columns:
                            s = close[sym].dropna()
                            if not s.empty:
                                prices_by_sym[sym] = float(s.iloc[-1])
                else:  # single symbol -> Close is a Series
                    s = close.dropna()
                    if not s.empty and dl_syms:
                        prices_by_sym[dl_syms[0]] = float(s.iloc[-1])
        except Exception:
            pass

        for t in stock_list:
            price = prices_by_sym.get(sym_for[t])
            result[t] = price if price is not None else fallback_prices.get(t, 0.0)
            _current_prices_cache[t] = result[t]

    return result

def get_benchmark_series(start_date: str, end_date: str) -> dict[str, float]:
    cache_key = f"{start_date}_{end_date}"
    if cache_key in _benchmark_cache:
        return _benchmark_cache[cache_key]

    # ^GSPC (the index) frequently times out / rate-limits on Yahoo. Try it first,
    # then fall back to liquid S&P 500 ETFs which resolve far more reliably.
    for symbol in ("^GSPC", "SPY", "^SPX", "VOO"):
        for attempt in range(2):
            try:
                df = yf.download(symbol, start=start_date, end=end_date,
                                 progress=False, auto_adjust=True)
                if df is None or df.empty or "Close" not in df:
                    continue
                close_series = df["Close"]
                # Single-ticker downloads can come back as a 1-col DataFrame
                if hasattr(close_series, "columns"):
                    close_series = close_series.iloc[:, 0]
                close_series = close_series.dropna()
                if close_series.empty:
                    continue

                # Normalize to base 100
                first_val = float(close_series.iloc[0])
                if first_val == 0:
                    continue
                series_dict = {str(idx.date()): (float(val) / first_val * 100)
                               for idx, val in close_series.items()}
                _benchmark_cache[cache_key] = series_dict
                print(f"Benchmark loaded from {symbol} ({len(series_dict)} points)")
                return series_dict
            except Exception as e:
                print(f"Benchmark fetch failed for {symbol} (attempt {attempt + 1}): {e}")
                continue

    print("Benchmark: all symbols failed; S&P 500 line will be empty.")
    _benchmark_cache[cache_key] = {}
    return {}

def get_historical_prices(tickers: list[str], start_date: str, end_date: str, source_map: dict = None,
                          asset_class_map: dict = None, symbol_map: dict = None) -> dict[str, pd.Series]:
    if source_map is None:
        source_map = {}
    if asset_class_map is None:
        asset_class_map = {}
    if symbol_map is None:
        symbol_map = {}

    result = {}
    to_fetch_stocks = set()
    to_fetch_crypto = set()

    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    days = (end_dt - start_dt).days
    if days <= 0:
        days = 1

    for t in tickers:
        cache_key = f"{t}_{start_date}_{end_date}"
        if cache_key in _historical_prices_cache:
            result[t] = _historical_prices_cache[cache_key]
            continue

        asset_type = classify_asset(t, source_map.get(t, ""), asset_class_map.get(t, ""))
        if asset_type == "Cash":
            # Cash is always 1.0, create a series
            idx = pd.date_range(start=start_dt, end=end_dt, freq='D')
            result[t] = pd.Series(1.0, index=idx)
        elif asset_type == "Crypto":
            to_fetch_crypto.add(t)
        else:
            to_fetch_stocks.add(t)

    # Fetch Crypto history.
    # Primary: yfinance "<TICKER>-EUR" in ONE batched call (no per-coin CoinGecko
    # requests, which used to exhaust the free-tier rate limit). CoinGecko's
    # per-coin market_chart is the fallback for coins yfinance does not cover.
    if to_fetch_crypto:
        crypto_list = list(to_fetch_crypto)
        resolved = set()
        try:
            syms = [f"{t}-EUR" for t in crypto_list]
            cdf = yf.download(syms, start=start_date, end=end_date, progress=False, auto_adjust=True)
            if cdf is not None and not cdf.empty and "Close" in cdf:
                close_df = cdf["Close"]
                for t in crypto_list:
                    sym = f"{t}-EUR"
                    series = None
                    if len(syms) == 1:
                        series = (close_df.iloc[:, 0] if hasattr(close_df, "columns") else close_df).dropna()
                    elif hasattr(close_df, "columns") and sym in close_df.columns:
                        series = close_df[sym].dropna()
                    if series is not None and not series.empty:
                        series.index = series.index.normalize()
                        result[t] = series
                        _historical_prices_cache[f"{t}_{start_date}_{end_date}"] = series
                        resolved.add(t)
        except Exception:
            pass

        for t in crypto_list:
            if t in resolved:
                continue
            cg_id = COINGECKO_ID_MAP.get(t, t.lower())
            try:
                resp = requests.get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=eur&days={days}", timeout=10)
                if resp.status_code == 200 and "prices" in resp.json():
                    data = resp.json()
                    df = pd.DataFrame(data["prices"], columns=["timestamp", "price"])
                    df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.normalize()
                    daily = df.groupby("date")["price"].last()
                    result[t] = daily
                    _historical_prices_cache[f"{t}_{start_date}_{end_date}"] = daily
                else:
                    result[t] = pd.Series(dtype=float)
            except Exception:
                result[t] = pd.Series(dtype=float)

    # Fetch Stocks history using resolved Yahoo symbols, keyed back to ticker.
    if to_fetch_stocks:
        stock_list = list(to_fetch_stocks)
        sym_for = {t: (symbol_map.get(t) or t) for t in stock_list}
        dl_syms = sorted({s for s in sym_for.values() if s})
        series_by_sym = {}
        try:
            df = yf.download(dl_syms, start=start_date, end=end_date, progress=False, auto_adjust=True)
            if df is not None and not df.empty and "Close" in df:
                close_df = df["Close"]
                if hasattr(close_df, "columns"):
                    for sym in dl_syms:
                        if sym in close_df.columns:
                            s = close_df[sym].dropna()
                            if not s.empty:
                                s.index = s.index.normalize()
                                series_by_sym[sym] = s
                else:  # single symbol
                    s = close_df.dropna()
                    if not s.empty and dl_syms:
                        s.index = s.index.normalize()
                        series_by_sym[dl_syms[0]] = s
        except Exception:
            pass

        for t in stock_list:
            s = series_by_sym.get(sym_for[t])
            result[t] = s if s is not None else pd.Series(dtype=float)
            if s is not None:
                _historical_prices_cache[f"{t}_{start_date}_{end_date}"] = s

    return result
