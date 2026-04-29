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

def get_current_prices(tickers: list[str], fallback_prices: dict = None, source_map: dict = None) -> dict[str, float]:
    result = {}
    if fallback_prices is None:
        fallback_prices = {}
    if source_map is None:
        source_map = {}

    to_fetch_stocks = set()
    to_fetch_crypto = set()

    for t in tickers:
        if t in _current_prices_cache:
            result[t] = _current_prices_cache[t]
            continue
            
        asset_type = classify_asset(t, source_map.get(t, ""))
        if asset_type == "Cash":
            result[t] = 1.0
            _current_prices_cache[t] = 1.0
        elif asset_type == "Crypto":
            to_fetch_crypto.add(t)
        else:
            to_fetch_stocks.add(t)

    # Fetch Crypto
    if to_fetch_crypto:
        cg_ids = [COINGECKO_ID_MAP.get(t, t.lower()) for t in to_fetch_crypto]
        try:
            resp = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={','.join(cg_ids)}&vs_currencies=eur", timeout=10)
            data = resp.json()
            for t in to_fetch_crypto:
                cg_id = COINGECKO_ID_MAP.get(t, t.lower())
                price = data.get(cg_id, {}).get("eur")
                if price is not None:
                    result[t] = price
                    _current_prices_cache[t] = price
                else:
                    result[t] = fallback_prices.get(t, 0.0)
                    _current_prices_cache[t] = result[t]
        except Exception:
            for t in to_fetch_crypto:
                result[t] = fallback_prices.get(t, 0.0)
                _current_prices_cache[t] = result[t]

    # Fetch Stocks
    if to_fetch_stocks:
        stock_list = list(to_fetch_stocks)
        try:
            df = yf.download(stock_list, period="5d", progress=False, auto_adjust=True)
            if not df.empty:
                # yfinance returns MultiIndex columns if multiple tickers
                if len(stock_list) > 1:
                    close_df = df["Close"]
                    for t in stock_list:
                        series = close_df[t].dropna()
                        if not series.empty:
                            result[t] = float(series.iloc[-1])
                            _current_prices_cache[t] = result[t]
                        else:
                            # Try fast_info
                            try:
                                result[t] = float(yf.Ticker(t).fast_info.last_price)
                                _current_prices_cache[t] = result[t]
                            except Exception:
                                result[t] = fallback_prices.get(t, 0.0)
                                _current_prices_cache[t] = result[t]
                else:
                    t = stock_list[0]
                    series = df["Close"].dropna()
                    if not series.empty:
                        result[t] = float(series.iloc[-1])
                        _current_prices_cache[t] = result[t]
                    else:
                        result[t] = fallback_prices.get(t, 0.0)
                        _current_prices_cache[t] = result[t]
            else:
                for t in stock_list:
                    result[t] = fallback_prices.get(t, 0.0)
        except Exception:
            for t in stock_list:
                result[t] = fallback_prices.get(t, 0.0)
                _current_prices_cache[t] = result[t]

    return result

def get_benchmark_series(start_date: str, end_date: str) -> dict[str, float]:
    cache_key = f"{start_date}_{end_date}"
    if cache_key in _benchmark_cache:
        return _benchmark_cache[cache_key]

    try:
        df = yf.download("^GSPC", start=start_date, end=end_date, progress=False, auto_adjust=True)
        if df.empty or "Close" not in df:
            return {}
        close_series = df["Close"].dropna()
        if close_series.empty:
            return {}
        
        # Normalize to 100
        first_val = float(close_series.iloc[0])
        # Format keys as YYYY-MM-DD
        series_dict = {str(idx.date()): (float(val) / first_val * 100) for idx, val in close_series.items()}
        _benchmark_cache[cache_key] = series_dict
        return series_dict
    except Exception:
        return {}

def get_historical_prices(tickers: list[str], start_date: str, end_date: str, source_map: dict = None) -> dict[str, pd.Series]:
    if source_map is None:
        source_map = {}
        
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
            
        asset_type = classify_asset(t, source_map.get(t, ""))
        if asset_type == "Cash":
            # Cash is always 1.0, create a series
            idx = pd.date_range(start=start_dt, end=end_dt, freq='D')
            result[t] = pd.Series(1.0, index=idx)
        elif asset_type == "Crypto":
            to_fetch_crypto.add(t)
        else:
            to_fetch_stocks.add(t)

    # Fetch Crypto history
    for t in to_fetch_crypto:
        cg_id = COINGECKO_ID_MAP.get(t, t.lower())
        try:
            resp = requests.get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=eur&days={days}", timeout=10)
            data = resp.json()
            if "prices" in data:
                # prices is list of [timestamp, price]
                df = pd.DataFrame(data["prices"], columns=["timestamp", "price"])
                df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.normalize()
                # Aggregate to daily closing
                daily = df.groupby("date")["price"].last()
                result[t] = daily
                _historical_prices_cache[f"{t}_{start_date}_{end_date}"] = daily
            else:
                result[t] = pd.Series(dtype=float)
        except Exception:
            result[t] = pd.Series(dtype=float)

    # Fetch Stocks history
    if to_fetch_stocks:
        stock_list = list(to_fetch_stocks)
        try:
            df = yf.download(stock_list, start=start_date, end=end_date, progress=False, auto_adjust=True)
            if not df.empty and "Close" in df:
                close_df = df["Close"]
                if len(stock_list) > 1:
                    for t in stock_list:
                        series = close_df[t].dropna()
                        # normalize index to dates
                        series.index = series.index.normalize()
                        result[t] = series
                        _historical_prices_cache[f"{t}_{start_date}_{end_date}"] = series
                else:
                    t = stock_list[0]
                    series = close_df.dropna()
                    series.index = series.index.normalize()
                    result[t] = series
                    _historical_prices_cache[f"{t}_{start_date}_{end_date}"] = series
        except Exception:
            for t in stock_list:
                result[t] = pd.Series(dtype=float)

    return result
