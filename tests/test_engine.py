"""Offline tests for the portfolio engine (no network: a fake market is injected)."""

import datetime as dt
import json
import unittest

import numpy as np
import pandas as pd

from engine import compute_portfolio, empty_portfolio
from engine.prices import PriceSeries
from engine import metrics


def tx(date, ticker, type_, qty, price=1.0, fee=0.0, source="XTB", **extra):
    d = {"date": dt.datetime.fromisoformat(date), "ticker": ticker, "type": type_, "qty": qty,
         "price": price, "fee": fee, "realized_pnl": None, "position_id": None,
         "market_price_at_export": None, "source": source, "currency": "EUR", "isin": None,
         "raw_symbol": None, "asset_class_hint": "", "uid": None, "account": None,
         "snapshot": False, "name": None}
    d.update(extra)
    return d


def cash(date, type_, amount, source="XTB"):
    return tx(date, "CASH", type_, amount, 1.0, source=source)


class FakeMarket:
    """prices: {key: {"YYYY-MM-DD": eur}} — trading-day observations."""

    def __init__(self, prices=None, bench=None):
        self.prices = prices or {}
        self.bench = bench

    @staticmethod
    def _series(points):
        s = pd.Series(points, dtype=float)
        s.index = pd.to_datetime(s.index)
        return s.sort_index()

    def daily_prices_eur(self, instruments, start, end):
        out = {}
        for inst in instruments:
            pts = self.prices.get(inst["key"])
            series = self._series(pts) if pts else pd.Series(dtype=float)
            out[inst["key"]] = PriceSeries(series, symbol=f"FAKE:{inst['ticker']}", currency="EUR")
        return out

    def benchmark_eur(self, start, end):
        return (self._series(self.bench) if self.bench else None), "fake benchmark"


def daily(start, end, f):
    """{date: f(i)} for every calendar day in [start, end]."""
    days = pd.date_range(start, end, freq="D")
    return {d.strftime("%Y-%m-%d"): f(i) for i, d in enumerate(days)}


TODAY = dt.date(2024, 12, 31)


class EngineTests(unittest.TestCase):

    def run_engine(self, txs, prices=None, bench=None, today=TODAY):
        return compute_portfolio(txs, market=FakeMarket(prices, bench), today=today)

    def test_empty(self):
        self.assertEqual(compute_portfolio([]), empty_portfolio())

    def test_buy_and_hold_value_pnl_and_xirr(self):
        txs = [cash("2024-01-01T10:00:00", "DEPOSIT", 1000),
               tx("2024-01-01T11:00:00", "AAPL", "BUY", 10, 100.0)]
        prices = {"XTB:AAPL": {"2024-01-01": 100.0, "2024-12-31": 110.0}}
        r = self.run_engine(txs, prices)
        k = r["kpis"]
        self.assertAlmostEqual(k["total_value"], 1100.0)
        self.assertAlmostEqual(k["unrealized_pnl"], 100.0)
        self.assertAlmostEqual(k["total_pnl"], 100.0)
        self.assertAlmostEqual(k["total_pnl_pct"], 10.0)
        self.assertAlmostEqual(k["unreconciled"], 0.0, places=6)
        self.assertAlmostEqual(k["twr"], 10.0, places=6)
        # one year (leap year: 365 days between flows) -> ~10% p.a.
        self.assertAlmostEqual(k["xirr"], 10.0, delta=0.1)
        self.assertEqual(r["positions"][0]["price_source"], "live")
        self.assertEqual(r["charts"]["net_worth"][-1], k["total_value"])

    def test_deposit_does_not_move_twr_but_moves_value(self):
        txs = [cash("2024-01-01T10:00:00", "DEPOSIT", 1000),
               tx("2024-01-01T11:00:00", "AAPL", "BUY", 10, 100.0),
               cash("2024-06-01T10:00:00", "DEPOSIT", 5000)]
        prices = {"XTB:AAPL": {"2024-01-01": 100.0, "2024-03-01": 110.0}}
        r = self.run_engine(txs, prices)
        self.assertAlmostEqual(r["kpis"]["twr"], 10.0, places=6)
        self.assertAlmostEqual(r["kpis"]["total_value"], 6100.0)
        self.assertAlmostEqual(r["charts"]["invested"][-1], 6000.0)

    def test_xirr_ignores_internal_trades(self):
        # Buys/sells are internal: XIRR must equal the deposit-based return.
        txs = [cash("2023-01-01T09:00:00", "DEPOSIT", 1000),
               tx("2023-01-01T10:00:00", "X", "BUY", 10, 100.0),
               tx("2023-06-01T10:00:00", "X", "SELL", 10, 105.0),
               tx("2023-06-02T10:00:00", "Y", "BUY", 5, 210.0)]
        prices = {"XTB:X": {"2023-01-01": 100.0, "2023-06-01": 105.0},
                  "XTB:Y": {"2023-06-02": 210.0, "2024-01-01": 220.0}}
        r = self.run_engine(txs, prices, today=dt.date(2024, 1, 1))
        expected_value = 1000 - 1000 + 1050 - 1050 + 5 * 220
        self.assertAlmostEqual(r["kpis"]["total_value"], expected_value)
        self.assertAlmostEqual(r["kpis"]["xirr"], (expected_value / 1000 - 1) * 100, delta=0.1)

    def test_accounting_identity_with_every_type(self):
        txs = [cash("2024-01-01T09:00:00", "DEPOSIT", 2000),
               tx("2024-01-02T10:00:00", "AAPL", "BUY", 10, 100.0, fee=2.0),
               tx("2024-02-01T10:00:00", "AAPL", "DIVIDEND", 5.0, 1.0),
               tx("2024-02-01T10:00:01", "AAPL", "WITHHOLDING_TAX", 0.75, 1.0),
               cash("2024-03-01T10:00:00", "INTEREST", 3.0),
               cash("2024-03-02T10:00:00", "FEE", 1.5),
               tx("2024-04-01T10:00:00", "AAPL", "SELL", 4, 120.0, fee=1.0),
               tx("2024-04-02T10:00:00", "BTC", "TRANSFER_IN", 0.01, 40000.0, source="Bitpanda",
                  asset_class_hint="Cryptocurrency"),
               tx("2024-05-01T10:00:00", "BTC", "REWARD", 0.001, 50000.0, source="Bitpanda",
                  asset_class_hint="Cryptocurrency"),
               tx("2024-06-01T10:00:00", "BTC", "TRANSFER_OUT", 0.005, 55000.0, source="Bitpanda",
                  asset_class_hint="Cryptocurrency"),
               cash("2024-07-01T10:00:00", "WITHDRAWAL", 300)]
        prices = {"XTB:AAPL": {"2024-01-02": 100.0, "2024-12-31": 130.0},
                  "Bitpanda:BTC": {"2024-04-02": 40000.0, "2024-12-31": 60000.0}}
        r = self.run_engine(txs, prices)
        k = r["kpis"]
        self.assertAlmostEqual(k["unreconciled"], 0.0, places=6)
        self.assertAlmostEqual(k["dividend_income"], 5.0)
        self.assertAlmostEqual(k["withholding_tax"], 0.75)
        self.assertAlmostEqual(k["interest_income"], 3.0)
        self.assertAlmostEqual(k["reward_income"], 50.0)
        self.assertAlmostEqual(k["total_fees"], 4.5)
        # contributions: 2000 + 400 (transfer in) - 275 (transfer out) - 300
        self.assertAlmostEqual(k["net_contributions"], 1825.0)
        btc = next(p for p in r["positions"] if p["ticker"] == "BTC")
        self.assertAlmostEqual(btc["qty"], 0.006)
        self.assertEqual(btc["asset_class"], "Crypto")
        # transfers/rewards are not trades
        self.assertEqual(r["analytics"]["closed_count"], 1)
        json.dumps(r, allow_nan=False)  # JSON-safe

    def test_positions_are_per_broker(self):
        txs = [cash("2024-01-01T09:00:00", "DEPOSIT", 1000, source="XTB"),
               cash("2024-01-01T09:00:00", "DEPOSIT", 1000, source="Trading212"),
               tx("2024-01-01T10:00:00", "VUSA", "BUY", 1, 80.0, source="XTB"),
               tx("2024-01-01T10:00:00", "VUSA", "BUY", 2, 90.0, source="Trading212")]
        prices = {"XTB:VUSA": {"2024-01-01": 85.0}, "Trading212:VUSA": {"2024-01-01": 85.0}}
        r = self.run_engine(txs, prices)
        by_src = {p["source"]: p for p in r["positions"]}
        self.assertEqual(set(by_src), {"XTB", "Trading212"})
        self.assertAlmostEqual(by_src["XTB"]["avg_cost"], 80.0)
        self.assertAlmostEqual(by_src["Trading212"]["avg_cost"], 90.0)

    def test_wrong_instrument_feed_is_rejected(self):
        txs = [cash("2024-01-01T09:00:00", "DEPOSIT", 100, source="Bitpanda"),
               tx("2024-01-01T10:00:00", "TOKEN", "BUY", 1000, 0.05, source="Bitpanda",
                  asset_class_hint="Cryptocurrency"),
               tx("2024-03-01T10:00:00", "TOKEN", "BUY", 1000, 0.04, source="Bitpanda",
                  asset_class_hint="Cryptocurrency")]
        prices = {"Bitpanda:TOKEN": {"2024-01-01": 45.0, "2024-03-01": 47.0, "2024-12-31": 60.0}}
        r = self.run_engine(txs, prices)
        rep = r["price_report"][0]
        self.assertEqual(rep["status"], "rejected")
        pos = r["positions"][0]
        self.assertAlmostEqual(pos["price"], 0.04)          # last trade price
        self.assertEqual(pos["price_source"], "cost")
        self.assertLess(max(r["charts"]["net_worth"]), 101)  # no spike
        self.assertTrue(any("No usable live price" in w["message"] for w in r["warnings"]))

    def test_minor_unit_feed_is_rescaled(self):
        txs = [cash("2024-01-01T09:00:00", "DEPOSIT", 100),
               tx("2024-01-01T10:00:00", "LLOY", "BUY", 100, 0.50)]
        prices = {"XTB:LLOY": {"2024-01-01": 50.0, "2024-12-31": 60.0}}  # pence-like scale
        r = self.run_engine(txs, prices)
        self.assertEqual(r["price_report"][0]["status"], "ok")
        self.assertAlmostEqual(r["positions"][0]["price"], 0.60)

    def test_large_genuine_gain_is_not_capped(self):
        txs = [cash("2016-01-04T09:00:00", "DEPOSIT", 500, source="Bitpanda"),
               tx("2016-01-04T10:00:00", "BTC", "BUY", 1, 400.0, source="Bitpanda",
                  asset_class_hint="Cryptocurrency")]
        prices = {"Bitpanda:BTC": {"2016-01-04": 410.0, "2024-12-31": 90000.0}}
        r = self.run_engine(txs, prices)
        self.assertAlmostEqual(r["positions"][0]["price"], 90000.0)

    def test_win_rate_counts_trades(self):
        txs = [cash("2024-01-01T09:00:00", "DEPOSIT", 10000),
               tx("2024-01-02T10:00:00", "A", "BUY", 10, 100.0),
               tx("2024-02-01T10:00:00", "A", "SELL", 5, 110.0),
               tx("2024-03-01T10:00:00", "A", "SELL", 5, 90.0),
               tx("2024-03-02T10:00:00", "B", "BUY", 1, 100.0),
               tx("2024-04-01T10:00:00", "B", "SELL", 1, 130.0)]
        prices = {"XTB:A": {"2024-01-02": 100.0}, "XTB:B": {"2024-03-02": 100.0}}
        a = self.run_engine(txs, prices)["analytics"]
        self.assertEqual(a["closed_count"], 3)
        self.assertAlmostEqual(a["win_rate"], 200 / 3)
        self.assertAlmostEqual(a["profit_factor"], (50 + 30) / 50)

    def test_withdrawal_is_not_a_drawdown(self):
        txs = [cash("2024-01-01T09:00:00", "DEPOSIT", 10000),
               tx("2024-01-01T10:00:00", "A", "BUY", 100, 100.0),
               tx("2024-06-01T10:00:00", "A", "SELL", 50, 100.0),
               cash("2024-06-01T11:00:00", "WITHDRAWAL", 5000)]
        prices = {"XTB:A": {"2024-01-01": 100.0}}
        r = self.run_engine(txs, prices)
        self.assertAlmostEqual(r["risk"]["max_drawdown"], 0.0, places=9)
        self.assertAlmostEqual(r["kpis"]["twr"], 0.0, places=9)

    def test_data_quality_warnings(self):
        txs = [cash("2024-01-01T09:00:00", "DEPOSIT", 10),
               tx("2024-01-01T10:00:00", "A", "BUY", 1, 100.0),   # cash goes to -90
               tx("2024-02-01T10:00:00", "A", "SELL", 3, 100.0)]  # sells 2 more than held
        r = self.run_engine(txs, {"XTB:A": {"2024-01-01": 100.0}})
        msgs = " ".join(w["message"] for w in r["warnings"])
        self.assertIn("more units than", msgs)
        self.assertEqual(r["positions"], [])

    def test_benchmark_is_base_100_and_forward_filled(self):
        txs = [cash("2024-01-05T09:00:00", "DEPOSIT", 100)]   # a Friday
        bench = {"2024-01-05": 50.0, "2024-01-08": 55.0}
        r = self.run_engine(txs, bench=bench, today=dt.date(2024, 1, 9))
        self.assertEqual(r["charts"]["benchmark"], [100.0, 100.0, 100.0, 110.0, 110.0])


    def test_holding_the_benchmark_matches_it_exactly(self):
        # Two deposits at different index levels, each fully invested in a fund
        # that IS the index: every like-for-like comparison must be zero.
        # (The index moves on non-deposit days: daily TWR assumes deposits arrive
        # at the start of the day, so a jump ON a deposit day is attributed
        # slightly differently — negligible with real ~1% daily moves.)
        idx = {"2023-01-02": 100.0, "2023-07-01": 110.0, "2024-01-01": 121.0}
        txs = [cash("2023-01-02T09:00:00", "DEPOSIT", 1000),
               tx("2023-01-02T10:00:00", "IDX", "BUY", 10, 100.0),
               cash("2023-07-03T09:00:00", "DEPOSIT", 1100),
               tx("2023-07-03T10:00:00", "IDX", "BUY", 10, 110.0)]
        r = self.run_engine(txs, {"XTB:IDX": idx}, bench=idx, today=dt.date(2024, 1, 2))
        b = r["benchmark"]
        self.assertAlmostEqual(r["kpis"]["total_value"], 2420.0)
        self.assertAlmostEqual(b["mirror_value"], 2420.0)
        self.assertAlmostEqual(b["value_difference"], 0.0, places=6)
        self.assertAlmostEqual(b["xirr_difference"], 0.0, places=4)
        self.assertAlmostEqual(b["cagr"], 21.0, places=6)
        self.assertAlmostEqual(b["twr_difference"], 0.0, places=6)
        self.assertEqual(r["charts"]["benchmark_value"][-1], 2420.0)

    def test_benchmark_comparison_without_benchmark_data(self):
        r = self.run_engine([cash("2024-01-01T09:00:00", "DEPOSIT", 100)])
        b = r["benchmark"]
        self.assertIsNone(b["mirror_value"])
        self.assertIsNone(b["xirr_difference"])
        self.assertIsNone(b["cagr"])


class MetricsTests(unittest.TestCase):

    def test_mirror_portfolio(self):
        nan = float("nan")
        prices = [nan, 100.0, 110.0, 121.0]
        # 1000 arrives before the index has a price (waits as cash), 1100 on day 2,
        # 605 withdrawn on day 3 (5 units)
        vals = metrics.mirror_values(prices, [1000.0, 0.0, 1100.0, 0.0], [0.0, 0.0, 0.0, 605.0])
        self.assertTrue(vals[0] != vals[0])          # NaN before first price
        self.assertAlmostEqual(vals[1], 1000.0)
        self.assertAlmostEqual(vals[2], 2200.0)      # 10 + 10 units at 110
        self.assertAlmostEqual(vals[3], 15 * 121.0)

    def test_cagr(self):
        prices = np.array([float("nan")] + [100.0] * 365 + [121.0])   # 365 days, +21%
        self.assertAlmostEqual(metrics.cagr(prices), 21.0)

    def test_flow_neutral_returns(self):
        values = [1000.0, 1100.0, 6100.0]
        r, capped = metrics.daily_returns(values, [1000.0, 0.0, 5000.0], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(r[1], 0.10)
        self.assertAlmostEqual(r[2], 6100 / 6100 - 1)
        self.assertEqual(capped, 0)

    def test_glitch_is_capped(self):
        r, capped = metrics.daily_returns([100.0, 1000.0], [100.0, 0.0], [0.0, 0.0])
        self.assertEqual(capped, 1)
        self.assertAlmostEqual(r[1], metrics.DAILY_RETURN_BOUNDS[1])


if __name__ == "__main__":
    unittest.main()
