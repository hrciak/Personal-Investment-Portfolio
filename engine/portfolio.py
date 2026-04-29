import datetime
import math
import numpy as np
import pandas as pd
from pyxirr import xirr

from parsers.base import classify_asset
from engine.prices import get_current_prices, get_benchmark_series, get_historical_prices

def compute_portfolio(transactions: list[dict]) -> dict:
    if not transactions:
        return _empty_portfolio()
        
    df = pd.DataFrame(transactions)
    df["date"] = pd.to_datetime(df["date"])
    # Gracefully handle mixed naive/aware parsing
    if str(df["date"].dtype).startswith("datetime64[ns, "):
        df["date"] = df["date"].dt.tz_convert(None)
    elif df["date"].apply(lambda x: getattr(x, 'tzinfo', None) is not None).any():
        df["date"] = df["date"].apply(lambda x: x.replace(tzinfo=None) if pd.notna(x) else x)
        
    df = df.sort_values("date", ascending=True).reset_index(drop=True)
    
    # Pre-compute source map
    source_map = df.drop_duplicates("ticker").set_index("ticker")["source"].to_dict()
    
    # 1. POSITIONS & CASH
    positions_data = {}
    cash_balance = 0.0
    dividend_income = 0.0
    withholding_tax = 0.0
    total_invested_gross = 0.0
    
    cash_flows = []
    dividends_per_ticker = {}
    
    for _, row in df.iterrows():
        dt = row["date"]
        ticker = row["ticker"]
        qty = row["qty"]
        price = row["price"]
        fee = row["fee"]
        tx_type = row["type"]
        real_pnl_val = row["realized_pnl"]
        
        cost = (qty * price) + fee if pd.notna(price) else 0.0
        proceeds = (qty * price) - fee if pd.notna(price) else 0.0
        
        if tx_type == "DEPOSIT":
            cash_balance += qty * price
            cash_flows.append((dt, -(qty * price)))
            continue
            
        elif tx_type == "WITHDRAWAL":
            cash_balance -= qty * price
            cash_flows.append((dt, qty * price))
            continue
            
        elif tx_type == "BUY":
            cash_balance -= cost
            if ticker != "CASH":
                if ticker not in positions_data:
                    positions_data[ticker] = {
                        "running_qty": 0.0, "running_cost_basis": 0.0,
                        "total_bought_qty": 0.0, "total_bought_value": 0.0,
                        "realized_pnl": 0.0, "realized_count": 0, "market_price_at_export": row.get("market_price_at_export")
                    }
                p = positions_data[ticker]
                p["running_qty"] += qty
                p["total_bought_qty"] += qty
                p["total_bought_value"] += cost
                p["running_cost_basis"] += cost
                total_invested_gross += cost
                cash_flows.append((dt, -cost))
                
        elif tx_type == "SELL":
            cash_balance += proceeds
            cash_flows.append((dt, proceeds))
            if ticker != "CASH" and ticker in positions_data:
                p = positions_data[ticker]
                avg_cost = p["total_bought_value"] / p["total_bought_qty"] if p["total_bought_qty"] > 0 else 0
                
                if pd.notna(real_pnl_val) and real_pnl_val is not None:
                    p["realized_pnl"] += real_pnl_val
                else:
                    p["realized_pnl"] += proceeds - (avg_cost * qty)
                    
                p["running_qty"] -= qty
                p["running_cost_basis"] -= (avg_cost * qty)
                p["realized_count"] += 1
                
        elif tx_type == "DIVIDEND":
            cash_balance += qty * price
            dividend_income += qty * price
            cash_flows.append((dt, qty * price))
            if ticker != "CASH":
                if ticker not in dividends_per_ticker:
                    dividends_per_ticker[ticker] = {"total_eur": 0.0, "last_date": dt, "count": 0}
                dividends_per_ticker[ticker]["total_eur"] += qty * price
                dividends_per_ticker[ticker]["last_date"] = dt
                dividends_per_ticker[ticker]["count"] += 1
                
        elif tx_type == "WITHHOLDING_TAX":
            cash_balance -= qty * price
            withholding_tax += qty * price
            cash_flows.append((dt, -(qty * price)))

    # Fetch live prices
    # Determine fallback prices from last transaction or market_price_at_export
    fallback = {}
    for ticker, p in positions_data.items():
        if p["running_qty"] > 0:
            if pd.notna(p["market_price_at_export"]):
                fallback[ticker] = float(p["market_price_at_export"])
            elif p["total_bought_qty"] > 0:
                fallback[ticker] = float(p["total_bought_value"] / p["total_bought_qty"])
    
    active_tickers = [t for t, p in positions_data.items() if p["running_qty"] > 0.0001]
    current_prices = get_current_prices(active_tickers, fallback, source_map)
    
    # Build final positions map
    open_positions = []
    total_market_value = 0.0
    total_realized_pnl = 0.0
    total_unrealized_pnl = 0.0
    allocation = {"Stock/ETF": 0.0, "Crypto": 0.0, "Cash": cash_balance}
    
    total_realized_pnl_only = sum(p["realized_pnl"] for p in positions_data.values())
    total_realized_with_divs = total_realized_pnl_only + dividend_income - withholding_tax

    for ticker, p in positions_data.items():
        total_realized_pnl += p["realized_pnl"]
        
        if p["running_qty"] > 0.0001:
            qty = p["running_qty"]
            price = current_prices.get(ticker, 0.0)
            mkt_val = qty * price
            cost_basis = p["running_cost_basis"]
            unrealized = mkt_val - cost_basis
            unrealized_pct = (unrealized / cost_basis * 100) if cost_basis > 0 else 0
            avg_cost = p["total_bought_value"] / p["total_bought_qty"] if p["total_bought_qty"] > 0 else 0
            
            total_market_value += mkt_val
            total_unrealized_pnl += unrealized
            
            asset_class = classify_asset(ticker, source_map.get(ticker, ""))
            if asset_class in allocation:
                allocation[asset_class] += mkt_val
                
            open_positions.append({
                "ticker": ticker,
                "asset_class": asset_class,
                "qty": qty,
                "avg_cost": avg_cost,
                "price": price,
                "market_value": mkt_val,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": unrealized_pct,
                "realized_pnl": p["realized_pnl"]
            })
            
            if ticker in dividends_per_ticker:
                dividends_per_ticker[ticker]["yield_pct"] = (dividends_per_ticker[ticker]["total_eur"] / cost_basis * 100) if cost_basis > 0 else 0

    total_portfolio_value = total_market_value + cash_balance
    total_pnl = total_realized_with_divs + total_unrealized_pnl
    total_pnl_pct = (total_pnl / total_invested_gross * 100) if total_invested_gross > 0 else 0

    total_invested_capital = total_portfolio_value - total_pnl

    # XIRR
    xirr_val = None
    if cash_flows:
        # Terminal flow
        terminal_flow = list(cash_flows)
        terminal_flow.append((datetime.datetime.now(), total_portfolio_value))
        try:
            xirr_res = xirr(terminal_flow)
            if xirr_res is not None:
                xirr_val = xirr_res * 100
        except Exception:
            xirr_val = None

    # NET WORTH TIMESERIES
    first_date = df["date"].min().date()
    today = datetime.date.today()
    
    date_range = pd.date_range(start=first_date, end=today, freq='D')
    hist_prices = get_historical_prices(active_tickers, str(first_date), str(today), source_map)
    bench_series = get_benchmark_series(str(first_date), str(today))
    
    nw_series = []
    inv_series = []
    
    # Fast timeseries build
    # Accumulate running qty and cash efficiently
    # df is sorted by date
    running_qtys = {t: 0.0 for t in active_tickers}
    running_cash = 0.0
    running_inv = 0.0
    
    tx_idx = 0
    num_tx = len(df)
    
    dates_list = [d.strftime('%Y-%m-%d') for d in date_range]
    benchmark_list = []
    
    temp_nw_values = []
    
    for current_dt in date_range:
        current_dt_end = current_dt + pd.Timedelta(days=1, microseconds=-1)
        
        while tx_idx < num_tx and df.iloc[tx_idx]["date"] <= current_dt_end:
            row = df.iloc[tx_idx]
            ticker = row["ticker"]
            qty = row["qty"]
            price = row["price"]
            tx_type = row["type"]
            fee = row["fee"]
            
            cost = (qty * price) + fee if pd.notna(price) else 0.0
            proceeds = (qty * price) - fee if pd.notna(price) else 0.0

            if tx_type == "DEPOSIT":
                running_cash += qty * price
            elif tx_type == "WITHDRAWAL":
                running_cash -= qty * price
            elif tx_type == "BUY":
                running_cash -= cost
                running_inv += cost
                if ticker in running_qtys:
                    running_qtys[ticker] += qty
            elif tx_type == "SELL":
                running_cash += proceeds
                running_inv -= proceeds
                if ticker in running_qtys:
                    running_qtys[ticker] -= qty
            elif tx_type == "DIVIDEND":
                running_cash += qty * price
            elif tx_type == "WITHHOLDING_TAX":
                running_cash -= qty * price
            
            tx_idx += 1
            
        current_dt_str = current_dt.strftime('%Y-%m-%d')
        daily_val = running_cash
        
        for t in active_tickers:
            r_qty = running_qtys[t]
            if r_qty > 0.0001:
                price_series = hist_prices.get(t)
                p_val = 0.0
                if price_series is not None and not price_series.empty:
                    # Forward fill logic per date
                    subset = price_series[price_series.index <= current_dt]
                    if not subset.empty:
                        p_val = subset.iloc[-1]
                    else:
                        p_val = fallback.get(t, 0.0)
                else:
                    p_val = fallback.get(t, 0.0)
                daily_val += r_qty * p_val
                
        nw_series.append({"date": current_dt_str, "value": daily_val})
        inv_series.append({"date": current_dt_str, "value": max(running_inv, 0)})
        temp_nw_values.append(daily_val)
        benchmark_list.append(bench_series.get(current_dt_str, None))

    # RISK METRICS
    volatility = None
    annualized_return = None
    sharpe = None
    max_dd = None
    
    if len(temp_nw_values) >= 60:
        s = pd.Series(temp_nw_values)
        pct_change = s.pct_change().dropna()
        if not pct_change.empty:
            vol = pct_change.std() * math.sqrt(252)
            ret = ((1 + pct_change.mean()) ** 252) - 1
            volatility = vol * 100
            annualized_return = ret * 100
            sharpe = (annualized_return - 2.5) / volatility if volatility > 0 else 0
            
            rolling_max = s.cummax()
            drawdown = (s - rolling_max) / rolling_max
            max_dd = drawdown.min() * 100

    # Format transactions for template
    tx_out = []
    rcash = 0.0
    for _, row in df.iterrows():
        qty = row["qty"]
        price = row["price"]
        fee = row["fee"]
        tx_type = row["type"]
        
        if tx_type == "DEPOSIT": rcash += qty * price
        elif tx_type == "WITHDRAWAL": rcash -= qty * price
        elif tx_type == "BUY": rcash -= (qty * price + fee)
        elif tx_type == "SELL": rcash += (qty * price - fee)
        elif tx_type == "DIVIDEND": rcash += qty * price
        elif tx_type == "WITHHOLDING_TAX": rcash -= qty * price
        
        # total column in table
        total_eur = 0.0
        if tx_type in ("BUY", "SELL"): total_eur = qty * price
        else: total_eur = qty * price
        
        tx_out.append({
            "date": row["date"].strftime("%d.%m.%Y %H:%M"),
            "source": row["source"],
            "ticker": row["ticker"],
            "type": row["type"],
            "qty": row["qty"],
            "price": row["price"],
            "total": total_eur,
            "fee": row["fee"],
            "running_cash": rcash,
            "asset_class": classify_asset(row["ticker"], row["source"])
        })
    tx_out.reverse()

    div_list = []
    for t, d in dividends_per_ticker.items():
        div_list.append({
            "ticker": t,
            "total_eur": d["total_eur"],
            "count": d["count"],
            "last_date": d["last_date"].strftime("%d.%m.%Y"),
            "yield_pct": d.get("yield_pct", 0.0)
        })

    return {
        "kpis": {
            "total_value": total_portfolio_value,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "xirr": xirr_val,
            "realized_pnl": total_realized_with_divs,
            "dividend_income": dividend_income
        },
        "risk": {
            "volatility": volatility,
            "sharpe_ratio": sharpe,
            "max_drawdown": max_dd,
            "beta": None
        },
        "allocation": allocation,
        "positions": sorted(open_positions, key=lambda x: x["market_value"], reverse=True),
        "transactions": tx_out,
        "dividends": sorted(div_list, key=lambda x: x["total_eur"], reverse=True),
        "charts": {
            "dates": dates_list,
            "net_worth": [x["value"] for x in nw_series],
            "invested": [x["value"] for x in inv_series],
            "benchmark": benchmark_list
        }
    }

def _empty_portfolio():
    return {
        "kpis": {"total_value": 0, "total_pnl": 0, "total_pnl_pct": 0, "xirr": None, "realized_pnl": 0, "dividend_income": 0},
        "risk": {"volatility": None, "sharpe_ratio": None, "max_drawdown": None, "beta": None},
        "allocation": {"Stock/ETF": 0, "Crypto": 0, "Cash": 0},
        "positions": [],
        "transactions": [],
        "dividends": [],
        "charts": {"dates": [], "net_worth": [], "invested": [], "benchmark": []}
    }
