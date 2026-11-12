"""
Ledger replay
=============

Replays the normalized transaction stream (see parsers.base.TX_TYPES) in date
order and keeps every running total the dashboard needs. It is the single
source of truth for cash, cost basis, realized P&L, income and external flows,
so every KPI is derived from the same bookkeeping.

Positions are keyed per broker ("XTB:AAPL"), because the same ticker held at
two brokers is two holdings (different cost bases, and the holdings table is
filterable by broker).

Accounting identity (exact when realized P&L is computed, not broker-reported):

    cash + market value - net contributions
        = realized + unrealized + dividends + interest + rewards - taxes - other fees
"""

from collections import defaultdict
from dataclasses import dataclass, field

QTY_EPS = 1e-6           # below this a position is treated as closed (dust)

INFLOW_TYPES = {"DEPOSIT", "SELL", "DIVIDEND", "INTEREST"}
OUTFLOW_TYPES = {"WITHDRAWAL", "BUY", "WITHHOLDING_TAX", "FEE"}
IN_KIND_TYPES = {"REWARD", "TRANSFER_IN", "TRANSFER_OUT"}
CASH_TICKERS = {"CASH", ""}


def position_key(source: str, ticker: str) -> str:
    return f"{source}:{ticker}"


@dataclass
class Position:
    key: str
    ticker: str
    source: str
    qty: float = 0.0
    cost: float = 0.0               # cost basis of the units currently held (EUR)
    bought_qty: float = 0.0
    bought_value: float = 0.0       # all-time acquisition cost incl. fees (EUR)
    realized: float = 0.0           # realized P&L (sales + transfers out)
    export_price: float | None = None   # broker's market price at statement export (EUR)
    name: str | None = None
    isin: str | None = None
    raw_symbol: str | None = None
    asset_class_hint: str = ""
    price_points: list = field(default_factory=list)   # [(date, EUR price)] from trades

    @property
    def avg_cost(self) -> float:
        return self.cost / self.qty if self.qty > QTY_EPS else 0.0

    @property
    def is_open(self) -> bool:
        return self.qty > QTY_EPS


@dataclass
class SourceTotals:
    invested: float = 0.0           # buy cost (turnover)
    realized_pnl: float = 0.0
    fees: float = 0.0
    dividends: float = 0.0
    cash: float = 0.0
    net_contributions: float = 0.0


class Ledger:
    def __init__(self):
        self.positions: dict[str, Position] = {}
        self.cash = 0.0
        self.net_contributions = 0.0
        self.total_invested = 0.0
        self.dividends = 0.0
        self.interest = 0.0
        self.rewards = 0.0
        self.withholding_tax = 0.0
        self.trade_fees = 0.0
        self.other_fees = 0.0
        self.trade_results: list[float] = []      # realized P&L per SELL
        self.by_source: dict[str, SourceTotals] = defaultdict(SourceTotals)
        self.dividends_by_ticker: dict[str, dict] = {}
        self.oversold: dict[str, float] = defaultdict(float)   # key -> units sold beyond holdings

    # ---------------------------------------------------------------- helpers
    def _position(self, tx) -> Position:
        key = position_key(tx["source"], tx["ticker"])
        pos = self.positions.get(key)
        if pos is None:
            pos = Position(key=key, ticker=tx["ticker"], source=tx["source"])
            self.positions[key] = pos
        pos.name = pos.name or tx.get("name")
        pos.isin = pos.isin or tx.get("isin")
        pos.raw_symbol = pos.raw_symbol or tx.get("raw_symbol")
        pos.asset_class_hint = pos.asset_class_hint or tx.get("asset_class_hint") or ""
        return pos

    def _reduce(self, pos: Position, qty: float) -> float:
        """Remove `qty` units at average cost; return the cost basis removed."""
        held = pos.qty
        take = min(qty, held)
        if qty > held + QTY_EPS:
            self.oversold[pos.key] += qty - held
        removed = pos.avg_cost * take if held > QTY_EPS else 0.0
        pos.qty = held - take
        pos.cost -= removed
        if pos.qty <= QTY_EPS:
            pos.qty, pos.cost = 0.0, 0.0
        return removed

    # ---------------------------------------------------------------- replay
    def apply(self, tx) -> dict:
        """Apply one transaction. Returns the external flow it represents:
        {"in": EUR contributed, "out": EUR withdrawn} (in-kind at FMV)."""
        t = tx["type"]
        qty, price, fee = tx["qty"], tx["price"], tx["fee"]
        value = qty * price
        src = self.by_source[tx["source"]]
        flow = {"in": 0.0, "out": 0.0}

        if t == "DEPOSIT":
            self._cash(src, value)
            self.net_contributions += value
            src.net_contributions += value
            flow["in"] = value
        elif t == "WITHDRAWAL":
            self._cash(src, -value)
            self.net_contributions -= value
            src.net_contributions -= value
            flow["out"] = value
        elif t == "BUY":
            cost = value + fee
            self._cash(src, -cost)
            pos = self._position(tx)
            pos.qty += qty
            pos.cost += cost
            pos.bought_qty += qty
            pos.bought_value += cost
            self._mark(pos, tx)
            self.total_invested += cost
            self.trade_fees += fee
            src.invested += cost
            src.fees += fee
        elif t == "SELL":
            proceeds = value - fee
            self._cash(src, proceeds)
            pos = self._position(tx)
            removed = self._reduce(pos, qty)
            broker = tx.get("realized_pnl")
            realized = broker if broker is not None else proceeds - removed
            pos.realized += realized
            self._mark(pos, tx)
            self.trade_results.append(realized)
            self.trade_fees += fee
            src.fees += fee
            src.realized_pnl += realized
        elif t == "DIVIDEND":
            self._cash(src, value)
            self.dividends += value
            src.dividends += value
            if tx["ticker"] not in CASH_TICKERS:
                d = self.dividends_by_ticker.setdefault(
                    tx["ticker"], {"total_eur": 0.0, "count": 0, "last_date": tx["date"]})
                d["total_eur"] += value
                d["count"] += 1
                d["last_date"] = max(d["last_date"], tx["date"])
        elif t == "WITHHOLDING_TAX":
            self._cash(src, -value)
            self.withholding_tax += value
        elif t == "INTEREST":
            self._cash(src, value)
            self.interest += value
        elif t == "FEE":
            self._cash(src, -value)
            self.other_fees += value
            src.fees += value
        elif t == "REWARD":
            pos = self._position(tx)
            pos.qty += qty
            pos.cost += value
            self._mark(pos, tx)
            self.rewards += value
        elif t == "TRANSFER_IN":
            pos = self._position(tx)
            pos.qty += qty
            pos.cost += value
            self._mark(pos, tx)
            self.net_contributions += value
            src.net_contributions += value
            flow["in"] = value
        elif t == "TRANSFER_OUT":
            pos = self._position(tx)
            removed = self._reduce(pos, qty)
            # Moving units out at FMV realizes their gain from the portfolio's
            # point of view (keeps the accounting identity exact). It is not a
            # trade, so it does not enter win/loss statistics.
            pos.realized += value - removed
            src.realized_pnl += value - removed
            self._mark(pos, tx)
            self.net_contributions -= value
            src.net_contributions -= value
            flow["out"] = value
        else:
            raise ValueError(f"unknown transaction type {t!r}")
        return flow

    def _cash(self, src: SourceTotals, delta: float) -> None:
        self.cash += delta
        src.cash += delta

    @staticmethod
    def _mark(pos: Position, tx) -> None:
        """Remember observed EUR prices (for validating and backfilling feeds)."""
        if tx["price"] > 0:
            pos.price_points.append((tx["date"], tx["price"]))
        mpe = tx.get("market_price_at_export")
        if mpe:
            pos.export_price = mpe

    # ---------------------------------------------------------------- totals
    @property
    def realized(self) -> float:
        return sum(p.realized for p in self.positions.values())

    @property
    def income_net(self) -> float:
        return self.dividends + self.interest + self.rewards - self.withholding_tax

    @property
    def total_fees(self) -> float:
        return self.trade_fees + self.other_fees
