from datetime import date

CASH_TICKERS = {"CASH", "EUR", "USD", "GBP", "CHF", "USDT", "USDC", "BUSD", "DAI"}
CRYPTO_TICKERS = {
    "BTC", "ETH", "SOL", "ADA", "DOT", "MATIC", "BNB", "XRP", "AVAX", "LINK", 
    "LTC", "DOGE", "ATOM", "ALGO", "UNI", "AAVE", "NEAR", "FTM", "HBAR", "ICP", 
    "APE", "EGLD"
}

def classify_asset(ticker: str, source: str = "", asset_class_str: str = "") -> str:
    """Classifies a ticker into Crypto, Stock/ETF, or Cash."""
    if not ticker:
        return "Stock/ETF"
    
    t_up = ticker.upper()
    if t_up in CASH_TICKERS:
        return "Cash"
    
    if t_up in CRYPTO_TICKERS:
        return "Crypto"
        
    if source.lower() == "bitpanda" and asset_class_str.lower() == "cryptocurrency":
        return "Crypto"
        
    return "Stock/ETF"

def create_transaction(
    date: date,
    ticker: str,
    tx_type: str,
    qty: float,
    price: float,
    fee: float = 0.0,
    realized_pnl: float = None,
    position_id: str = None,
    market_price_at_export: float = None,
    source: str = "",
    currency: str = "EUR"
) -> dict:
    return {
        "date": date,
        "ticker": ticker.upper() if ticker else "UNKNOWN",
        "type": tx_type.upper(),
        "qty": float(qty) if qty else 0.0,
        "price": float(price) if price else 0.0,
        "fee": float(fee) if fee else 0.0,
        "realized_pnl": float(realized_pnl) if realized_pnl is not None else None,
        "position_id": position_id,
        "market_price_at_export": float(market_price_at_export) if market_price_at_export is not None else None,
        "source": source,
        "currency": currency.upper()
    }
