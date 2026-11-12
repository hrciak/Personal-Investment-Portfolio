"""Ingestion-layer tests. All fixtures are synthetic (built in a temp dir)."""

import csv
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import openpyxl

from parsers import bitpanda, etoro, trading212, xtb
from parsers.base import classify_asset, create_transaction, strip_exchange_suffix
from parsers.loader import load_statements


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _save(path, sheets):
    """sheets: list of (name, rows)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(list(r))
    wb.save(path)
    return path


def _old_block(title, period, generated, holder="Jan Novák", account="123456"):
    pad = [None] * 4
    return [
        [],
        [None, *pad, "Name and surname", None, None, "Account", None, None, "Currency",
         None, None, None, None, None, None, None, None, generated],
        [None, *pad, holder, None, None, account, None, None, "EUR"],
        [None, *pad, "Balance", None, None, "Equity"],
        [None, *pad, 10.5, None, None, 100.0],
        [None, title],
        [None, period],
    ]


OLD_CLOSED_HDR = [None, "Position", "Symbol", "Type", "Volume", "Open time", "Open price",
                  "Close time", "Close price", "Open origin", "Close origin", "Purchase value",
                  "Sale value", "SL", "TP", "Margin", "Commission", "Swap", "Rollover",
                  "Gross P/L", "Comment"]
OLD_OPEN_HDR = [None, "Position", "Symbol", "Type", "Volume", "Open time", "Open price",
                "Market price", "Purchase value", "SL", "TP", "Margin", "Commission", "Swap",
                "Rollover", "Gross P/L", "Comment"]
OLD_CASH_HDR = [None, "ID", "Type", "Time", "Comment", "Symbol", "Amount"]


def build_xtb_old(path, holder="Jan Novák", account="123456"):
    gen = datetime(2025, 7, 10, 17, 54, 35)
    period = "01/01/2024 - 10/07/2025"
    closed = _old_block("CLOSED POSITION HISTORY ", period, gen, holder, account) + [
        OLD_CLOSED_HDR,
        # winter open (UTC+1), summer close (UTC+2); prices in USD, values in EUR
        [None, 111, "BRK.B.US", "BUY", 2.0, datetime(2025, 1, 15, 10, 0), 400.0,
         datetime(2025, 7, 1, 15, 30), 420.0, "x", "x", 680.0, 720.0, None, None, 0.0,
         -1.0, 0.0, 0.0, 40.0, ""],
        [None, "Total", None, None, None, None, None, None, None, None, None, None, None,
         None, None, None, 0.0, 0.0, 0.0, 40.0],
    ]
    opened = _old_block("OPEN POSITION HISTORY ", datetime(2025, 7, 10), gen, holder, account) + [
        OLD_OPEN_HDR,
        [None, 222, "VUSA.DE", "BUY", 3.0, datetime(2025, 3, 1, 9, 0), 100.0, 110.0, 300.0,
         None, None, 0.0, 0.0, 0.0, 0.0, 30.0, ""],
        [None, "Total", None, None, None, None, None, None, None, None, None, 0.0, 0.0, 0.0, 30.0],
    ]
    t = datetime(2025, 2, 1, 12, 0)
    cash = _old_block("CASH OPERATION HISTORY ", period, gen, holder, account) + [
        OLD_CASH_HDR,
        [None, 1, "deposit", t, "Deposit", None, 500.0],
        [None, 2, "DIVIDENT", t, "VUSA.DE EUR 0.33/ SHR", "VUSA.DE", 1.0],
        [None, 3, "Withholding Tax", t, "VUSA.DE WHT 15%", "VUSA.DE", -0.15],
        [None, 4, "Free-funds Interest Tax", t, "", None, -0.05],
        [None, 5, "Free-funds Interest", t, "", None, 0.3],
        [None, 6, "Sec Fee", t, "", "BRK.B.US", -0.02],
        [None, 7, "Stock purchase", t, "OPEN BUY", "VUSA.DE", -300.0],
        [None, 8, "fractional shares", t, "split", "NKLA.US", 0.5],
        [None, "Total", 500.0, "EUR"],
    ]
    return _save(path, [("CLOSED POSITION HISTORY", closed), ("OPEN POSITION 10072025", opened),
                        ("CASH OPERATION HISTORY", cash)])


NEW_CLOSED_HDR = ["Instrument", "Ticker", "Category", "Type", "Volume", "Open Price",
                  "Open Time (UTC)", "Close Price", "Close Time (UTC)", "Product", "Profit/Loss",
                  "Gross Profit", "Purchase Value", "Sale Value", "Stop Loss", "Take Profit",
                  "Commission", "Margin", "Swap", "Rollover", "Open Conversion Rate",
                  "Close Conversion Rate", "Close Origin", "Position ID", "Comment"]
NEW_CASH_HDR = ["Type", "Instrument", "Ticker", "Category", "Time", "Amount", "ID", "Comment",
                "Product", "Position ID"]
NEW_OPEN_HDR = ["Product", "Instrument/Position", "Ticker", "Category", "Type", "Volume", "Value",
                "Current price", "Open price", "Open time (UTC)", "Stop Loss", "Take Profit",
                "Net Profit %", "Net Profit", "Gross Profit", "Margin", "Open Commission", "Swap",
                "Rollover"]


def build_xtb_new(path, account, date_from, date_to, positions=(), cash=(), closed=()):
    """positions: (pid, ticker, name, vol, value_eur, gross_eur, open_time_utc)."""
    head = lambda title: [["Account number", account], [title, ""],
                          ["Date from (UTC)", date_from], ["Date to (UTC)", date_to]]
    closed_rows = head("Closed Positions") + [NEW_CLOSED_HDR, *closed, ["Profit/loss"]]
    cash_rows = head("Cash Operations") + [NEW_CASH_HDR, *cash, ["Total", None, None, None, None, 0.0]]
    open_rows = [["Account number", account], ["Open Positions", ""],
                 ["Data as of report generated", date_to],
                 ["Product", "Metric", "Amount", "Currency"],
                 ["My Trades", "Open position value", 1.0, "EUR"], [], NEW_OPEN_HDR]
    for pid, ticker, name, vol, value, gross, opened in positions:
        open_rows.append(["My Trades", name, ticker, "STOCK", "", vol, value, "", 1.0, "", "", "",
                          0.0, gross, gross, "", "", "", ""])
        open_rows.append(["My Trades", pid, ticker, "", "BUY", vol, value, 2.0, 1.0, opened,
                          "", "", 0.0, gross, gross, "", "", "", ""])
    return _save(path, [("Closed Positions", closed_rows), ("Cash Operations", cash_rows),
                        ("Open Positions", open_rows)])


BP_HEADER = ["Transaction ID", "Timestamp", "Transaction Type", "In/Out", "Amount Fiat", "Fiat",
             "Amount Asset", "Asset", "Asset market price", "Asset market price currency",
             "Asset class", "Product ID", "Fee", "Fee asset", "Fee percent", "Spread",
             "Spread Currency", "Tax Fiat"]


def bp_row(tx_id, ts, kind, direction, fiat, units, asset, cls="Cryptocurrency", fee="-",
           fee_asset="-", price="-"):
    return [tx_id, ts, kind, direction, fiat, "EUR", units, asset, price, "EUR", cls, "1", fee,
            fee_asset, "-", "-", "-", "-"]


def build_bitpanda(path, rows, holder="Jan Novák"):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write('"Disclaimer: All data is without guarantee, errors and changes are reserved."\n')
        f.write(f'"{holder}, 1990-01-01"\n')
        f.write("someone@example.com\n")
        f.write('"Account opened at: 2024-01-01T09:00:00+01:00"\n')
        f.write('"Venue: Bitpanda"\n')
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(BP_HEADER)
        w.writerows(rows)
    return path


T212_HEADER = ["Action", "Time", "ISIN", "Ticker", "Name", "Notes", "ID", "No. of shares",
               "Price / share", "Currency (Price / share)", "Exchange rate", "Result",
               "Currency (Result)", "Total", "Currency (Total)", "Withholding tax",
               "Currency (Withholding tax)", "Stamp duty reserve tax",
               "Currency (Stamp duty reserve tax)", "Currency conversion fee",
               "Currency (Currency conversion fee)", "French transaction tax",
               "Currency (French transaction tax)"]


def t212_row(**kw):
    return [kw.get(c, "") for c in T212_HEADER]


def build_t212(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(T212_HEADER)
        w.writerows(rows)
    return path


def build_etoro(path, with_dividend_sheet=True):
    details = [["Podrobnosti"], ["Jméno", "Celkové částky"], ["Jméno", "Petr  Svoboda"],
               ["Uživatelské jméno", "petr1"], ["Měna", "USD/EUR"],
               ["Datum vytvoření", "30/04/2026 19:45:39"],
               ["Datum zahájení", "01/04/2026 00:00:00"],
               ["Datum ukončení", "29/04/2026 23:59:59"]]
    closed = [["ID pozice", "Akce", "Long / Short", "Částka", "Jednotky", "Datum otevření",
               "Datum uzavření", "Pákový efekt", "Zisk (USD)", "Zisk (EUR)", "Otevírací kurz",
               "Uzavírací kurz", "Typ", "ISIN"],
              ["100", "Apple Inc (AAPL)", "Long", 100, "0.5", "10/04/2026 14:00:00",
               "20/04/2026 14:00:00", "1", 10.0, 8.5, 200.0, 220.0, "Cenné papíry", "US0378331005"],
              ["101", "Tesla Inc (TSLA)", "Short", 100, "1", "11/04/2026 14:00:00",
               "21/04/2026 14:00:00", "1", -5.0, -4.25, 100.0, 105.0, "Cenné papíry", "-"]]
    act_hdr = ["Datum", "Napište", "Podrobnosti", "Částka", "Jednotky",
               "Změna realizovaného kapitálu", "Realizovaný kapitál", "Zůstatek", "ID pozice",
               "Typ aktiva", "NWA"]
    activity = [act_hdr,
                ["01/04/2026 10:00:00", "Vklad", "170.00 EUR CreditCard", 197.5, "-", 0, 0, 0, "-", "-", 0],
                ["01/04/2026 10:00:00", "Poplatek za směnu měny při vkladu", "-", -2.5, "-", 0, 0, 0, "-", "-", 0],
                ["10/04/2026 14:00:00", "Otevřená pozice", "AAPL/USD", 100, "0.5", 0, 0, 0, "100", "x", 0],
                ["15/04/2026 14:00:00", "Otevřená pozice", "MSFT/USD", 40, "0.1", 0, 0, 0, "102", "x", 0],
                ["16/04/2026 10:00:00", "Dividenda", "MSFT/USD", 0.5, "-", 0, 0, 0, "102", "x", 0],
                ["20/04/2026 14:00:00", "Zisk/ztráta z obchodu", "AAPL/USD", 110, "0.5", 0, 0, 0, "100", "x", 0],
                ["20/04/2026 14:00:00", "Provize", "Zavřít", -1, "-", 0, 0, 0, "100", "x", 0]]
    div = [["Datum platby", "Název nástroje", "Čistá přijatá dividenda (USD)", "Net dividends",
            "Currency", "Čistá přijatá dividenda (EUR)", "Sazba srážkové daně (%)",
            "Částka srážkové daně (USD)", "Částka srážkové daně (EUR)", "ID pozice", "Napište",
            "ISIN"]]
    if with_dividend_sheet:
        div.append(["16/04/2026 10:00:00", "Microsoft (MSFT)", 0.5, 0.5, "USD", 0.425, 15,
                    0.09, 0.0765, "102", "x", "US5949181045"])
    return _save(path, [("Přehled účtu", details), ("Zavřené pozice", closed),
                        ("Aktivita na účtu", activity), ("Dividendy", div),
                        ("Finanční shrnutí", [["Jméno", "Částka v (USD)"]])])


def by_type(txs, tx_type):
    return [t for t in txs if t["type"] == tx_type]


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)


# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------

class BaseTests(unittest.TestCase):
    def test_tz_aware_dates_become_naive_utc(self):
        t = create_transaction("2024-03-01T10:00:00+01:00", "btc", "buy", 1, 2)
        self.assertEqual(t["date"], datetime(2024, 3, 1, 9, 0))
        self.assertIsNone(t["date"].tzinfo)
        aware = datetime(2024, 7, 1, 12, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(create_transaction(aware, "X", "SELL", 1, 1)["date"], datetime(2024, 7, 1, 10))

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            create_transaction(datetime(2024, 1, 1), "X", "SPLIT", 1, 1)
        with self.assertRaises(ValueError):
            create_transaction("not a date", "X", "BUY", 1, 1)
        with self.assertRaises(ValueError):
            create_transaction(None, "X", "BUY", 1, 1)

    def test_contract_fields(self):
        t = create_transaction(datetime(2024, 1, 1), "vusa", "buy", -2, 10, fee=-0.5, isin=" ie00b3xxrp09 ",
                               uid="u1", account="a", snapshot=1, name=" Vanguard ")
        self.assertEqual((t["ticker"], t["qty"], t["fee"], t["isin"], t["snapshot"], t["name"]),
                         ("VUSA", 2.0, 0.5, "IE00B3XXRP09", True, "Vanguard"))
        self.assertEqual(set(t), {"date", "ticker", "type", "qty", "price", "fee", "realized_pnl",
                                  "position_id", "market_price_at_export", "source", "currency",
                                  "isin", "raw_symbol", "asset_class_hint", "uid", "account",
                                  "snapshot", "name"})

    def test_classify_and_suffix(self):
        self.assertEqual(classify_asset("USDC"), "Crypto")
        self.assertEqual(classify_asset("EUR"), "Cash")
        self.assertEqual(classify_asset("XYZ", "Bitpanda", "Cryptocurrency"), "Crypto")
        self.assertEqual(classify_asset("AAPL"), "Stock/ETF")
        self.assertEqual(strip_exchange_suffix("BRK.B.US"), "BRK.B")
        self.assertEqual(strip_exchange_suffix("VUSA.DE"), "VUSA")
        self.assertEqual(strip_exchange_suffix("BRK.B"), "BRK.B")


# ---------------------------------------------------------------------------
# XTB
# ---------------------------------------------------------------------------

class XtbTests(TempDirCase):
    def test_old_layout(self):
        st = xtb.parse(build_xtb_old(self.path("old.xlsx")))
        self.assertEqual((st.account, st.holder, st.reported_cash), ("123456", "Jan Novák", 10.5))
        self.assertEqual(st.generated_at, datetime(2025, 7, 10, 15, 54, 35))   # CEST -> UTC
        buy = next(t for t in st.transactions if t["type"] == "BUY" and not t["snapshot"])
        sell = by_type(st.transactions, "SELL")[0]
        self.assertEqual(buy["ticker"], "BRK.B")
        self.assertEqual(buy["raw_symbol"], "BRK.B.US")
        self.assertAlmostEqual(buy["price"], 340.0)            # EUR purchase value / volume
        self.assertEqual(buy["date"], datetime(2025, 1, 15, 9, 0))   # CET -> UTC
        self.assertEqual(sell["date"], datetime(2025, 7, 1, 13, 30))  # CEST -> UTC
        self.assertAlmostEqual(sell["price"], 360.0)
        self.assertAlmostEqual(sell["fee"], 1.0)
        self.assertAlmostEqual(sell["realized_pnl"], 39.0)     # gross - |commission|
        snap = next(t for t in st.transactions if t["snapshot"])
        self.assertEqual((snap["ticker"], snap["position_id"], snap["uid"]),
                         ("VUSA", "222", "xtb:123456:pos:222:open"))
        self.assertAlmostEqual(snap["market_price_at_export"], 110.0)

    def test_old_cash_mapping(self):
        st = xtb.parse(build_xtb_old(self.path("old.xlsx")))
        cash = {t["uid"]: (t["type"], t["ticker"], round(t["qty"], 4)) for t in st.transactions
                if t["uid"] and ":cash:" in t["uid"]}
        self.assertEqual(cash, {
            "xtb:123456:cash:1": ("DEPOSIT", "CASH", 500.0),
            "xtb:123456:cash:2": ("DIVIDEND", "VUSA", 1.0),
            "xtb:123456:cash:3": ("WITHHOLDING_TAX", "VUSA", 0.15),
            "xtb:123456:cash:4": ("WITHHOLDING_TAX", "CASH", 0.05),   # interest tax, not interest
            "xtb:123456:cash:5": ("INTEREST", "CASH", 0.3),
            "xtb:123456:cash:6": ("FEE", "BRK.B", 0.02),
            "xtb:123456:cash:8": ("INTEREST", "NKLA", 0.5),           # unknown type, by sign
        })                                                            # 7 = stock purchase: skipped
        self.assertTrue(any("fractional shares" in w for w in st.warnings))

    def test_new_layout(self):
        path = build_xtb_new(
            self.path("EUR_1.xlsx"), "999", datetime(2026, 10, 1), datetime(2026, 11, 1, 12),
            positions=[("555", "AMD.US", "AMD", 2.0, 300.0, 100.0, datetime(2025, 3, 5, 14, 30))],
            cash=[["Deposit", "", "", "", datetime(2026, 10, 2), 100.0, "77", "", "My Trades", ""],
                  ["Dividend", "ASML", "ASML.NL", "STOCK", datetime(2026, 10, 3), 0.59, "78", "", "", ""],
                  ["Withholding tax", "ASML", "ASML.NL", "STOCK", datetime(2026, 10, 3), -0.09, "79", "", "", ""],
                  ["Stock purchase", "AMD", "AMD.US", "STOCK", datetime(2026, 10, 4), -50.0, "80", "", "", "555"]],
            closed=[["Nike", "NKE.US", "STOCK", "BUY", 1.0, 70.0, datetime(2026, 10, 5), 80.0,
                     datetime(2026, 10, 20), "My Trades", 8.0, 9.0, 60.0, 69.0, "", "", -1.0, "",
                     0.0, 0.0, "", "", "", "666", ""]])
        st = xtb.parse(path)
        self.assertEqual((st.account, st.period_start, st.snapshot_at),
                         ("999", datetime(2026, 10, 1), datetime(2026, 11, 1, 12)))
        snap = next(t for t in st.transactions if t["snapshot"])
        self.assertAlmostEqual(snap["price"], 100.0)               # (Value - Gross Profit) / vol
        self.assertAlmostEqual(snap["market_price_at_export"], 150.0)
        self.assertEqual((snap["ticker"], snap["name"], snap["date"]), ("AMD", "AMD", datetime(2025, 3, 5, 14, 30)))
        sell = by_type(st.transactions, "SELL")[0]
        self.assertAlmostEqual(sell["realized_pnl"], 8.0)           # Profit/Loss column
        self.assertAlmostEqual(sell["price"], 69.0)
        self.assertEqual(sorted(t["type"] for t in st.transactions if ":cash:" in (t["uid"] or "")),
                         ["DEPOSIT", "DIVIDEND", "WITHHOLDING_TAX"])

    def test_detection_new_xtb_vs_etoro(self):
        new_xtb = build_xtb_new(self.path("EUR_x.xlsx"), "1", datetime(2026, 1, 1), datetime(2026, 2, 1))
        et = build_etoro(self.path("et.xlsx"))
        eng = _save(self.path("en.xlsx"), [("Account Summary", [["Name", "A B"]]),
                                           ("Open Positions", [["Position ID"]]),
                                           ("Account Activity", [["Date", "Type"]])])
        self.assertTrue(xtb.detect(new_xtb))
        self.assertFalse(etoro.detect(new_xtb))
        self.assertTrue(etoro.detect(et))
        self.assertFalse(xtb.detect(et))
        self.assertTrue(etoro.detect(eng))
        self.assertFalse(xtb.detect(eng))
        self.assertTrue(xtb.detect(build_xtb_old(self.path("old.xlsx"))))


# ---------------------------------------------------------------------------
# Bitpanda
# ---------------------------------------------------------------------------

class BitpandaTests(TempDirCase):
    def _files(self):
        common = [
            bp_row("D1", "2024-01-01T10:00:00+01:00", "deposit", "incoming", "100.00", "-", "EUR", cls="Fiat",
                   fee="0.00", fee_asset="EUR"),
            bp_row("B1", "2024-01-02T10:00:00+01:00", "buy", "outgoing", "50.00", "0.00100000", "BTC",
                   fee="0.01", fee_asset="EUR", price="49000"),
            bp_row("CD1", "2024-02-15T10:00:00+01:00", "deposit", "incoming", "1000.00", "0.50000000", "ETH",
                   fee="0.00100000", fee_asset="ETH"),
            bp_row("X1", "2024-04-01T10:00:00+02:00", "transfer", "incoming", "2.00", "0.20000000", "SOL"),
        ]
        june = common + [
            bp_row("R1", "2024-02-01T10:00:00+01:00", "reward", "incoming", "0.10", "0.01000000", "ETH"),
            bp_row("S1", "2024-03-01T10:00:01+01:00", "transfer(stake)", "incoming", "900.00", "0.50000000", "ETH"),
        ]
        nov = common + [
            bp_row("R1", "2024-02-01T10:00:00+01:00", "reward", "incoming", "0.00", "0.01000000", "ETH"),
            bp_row("T1", "2024-03-01T10:00:00+01:00", "transfer", "outgoing", "900.00", "0.50000000", "ETH"),
            bp_row("SE1", "2024-05-01T10:00:00+02:00", "sell", "incoming", "30.00", "0.00050000", "BTC"),
        ]
        build_bitpanda(self.path("bitpanda-trades-2026-06-14-18-39.csv"), june)
        build_bitpanda(self.path("bitpanda-trades-2026-11-12-19-23.csv"), nov)

    def test_row_mapping(self):
        self._files()
        st = bitpanda.parse(self.path("bitpanda-trades-2026-11-12-19-23.csv"))
        self.assertEqual(st.holder, "Jan Novák")
        self.assertEqual(st.period_start, datetime(2024, 1, 1, 8, 0))
        buy = by_type(st.transactions, "BUY")[0]
        self.assertAlmostEqual(buy["price"], 50000.0)          # Amount Fiat / Amount Asset
        self.assertAlmostEqual(buy["fee"], 0.01)
        dep = by_type(st.transactions, "TRANSFER_IN")
        self.assertIn(("ETH", 0.5), [(t["ticker"], t["qty"]) for t in dep])   # deposit net of fee already
        self.assertEqual(by_type(st.transactions, "DEPOSIT")[0]["qty"], 100.0)

    def test_merge_pairing_and_newest_wins(self):
        self._files()
        res = load_statements(self.dir)
        txs = res.transactions
        reward_eth = [t for t in by_type(txs, "REWARD") if t["ticker"] == "ETH"]
        self.assertEqual(len(reward_eth), 1)
        self.assertEqual(reward_eth[0]["price"], 0.0)            # Nov export revised R1
        # T1 (Nov) + S1 (June) are one internal move straddling the two files
        self.assertFalse(any(t["uid"] in ("bitpanda:T1", "bitpanda:S1") for t in txs))
        sol = [t for t in txs if t["ticker"] == "SOL"]
        self.assertEqual([t["type"] for t in sol], ["REWARD"])    # unmatched credit = reward
        self.assertEqual([t["uid"] for t in by_type(txs, "TRANSFER_IN")], ["bitpanda:CD1"])
        self.assertEqual(len(by_type(txs, "DEPOSIT")), 1)
        self.assertEqual(len(by_type(txs, "SELL")), 1)
        self.assertFalse(any(k.startswith("_") for t in txs for k in t))
        self.assertTrue(any(w["message"].startswith("Bitpanda: 1 internal") for w in res.warnings))


# ---------------------------------------------------------------------------
# Trading 212
# ---------------------------------------------------------------------------

class Trading212Tests(TempDirCase):
    def test_fees_income_and_dividends(self):
        rows = [
            t212_row(Action="Deposit", Time="2024-01-01 10:00:00", Notes="Bank Transfer", ID="d1",
                     Total="100.00", **{"Currency (Total)": "EUR"}),
            t212_row(Action="Deposit", Time="2024-01-01 10:01:00", Notes="Free Shares Promotion", ID="d2",
                     Total="10.00", **{"Currency (Total)": "EUR"}),
            t212_row(Action="Market buy", Time="2024-01-02 10:00:00", ISIN="IE00B3XXRP09", Ticker="VUSA",
                     ID="b1", **{"No. of shares": "1", "Price / share": "70", "Currency (Price / share)": "GBP",
                                 "Exchange rate": "0.85", "Total": "82.37", "Currency (Total)": "EUR",
                                 "Currency conversion fee": "0.02", "Currency (Currency conversion fee)": "EUR"}),
            t212_row(Action="Market sell", Time="2024-02-01 10:00:00", ISIN="IE00B3XXRP09", Ticker="VUSA",
                     ID="s1", Result="7.68", **{"No. of shares": "1", "Price / share": "76.5",
                                                "Currency (Price / share)": "GBP", "Exchange rate": "0.85",
                                                "Currency (Result)": "EUR", "Total": "90.00",
                                                "Currency (Total)": "EUR", "Currency conversion fee": "0.03",
                                                "Currency (Currency conversion fee)": "EUR"}),
            t212_row(Action="Dividend (Dividend)", Time="2024-03-01 10:00:00", Ticker="AAPL",
                     **{"No. of shares": "2", "Price / share": "0.35", "Currency (Price / share)": "USD",
                        "Exchange rate": "1.10", "Total": "0.70", "Currency (Total)": "EUR",
                        "Withholding tax": "0.30", "Currency (Withholding tax)": "USD"}),
            t212_row(Action="Interest on cash", Time="2024-03-02 10:00:00", ID="i1", Total="0.05",
                     **{"Currency (Total)": "EUR"}),
            t212_row(Action="Card debit", Time="2024-03-03 10:00:00", ID="c1", Total="5.00",
                     **{"Currency (Total)": "EUR"}),
            t212_row(Action="Stock split open", Time="2024-03-04 10:00:00", Ticker="AAPL"),
        ]
        st = trading212.parse(build_t212(self.path("Trading212-from_2024-01-01_to_2024-12-31_X.csv"), rows))
        buy, sell = by_type(st.transactions, "BUY")[0], by_type(st.transactions, "SELL")[0]
        self.assertAlmostEqual(buy["qty"] * buy["price"] + buy["fee"], 82.37)    # == Total, fee once
        self.assertAlmostEqual(sell["qty"] * sell["price"] - sell["fee"], 90.00)
        self.assertAlmostEqual(sell["realized_pnl"], 7.68 - 0.03 - 0.02)         # net of both fees
        self.assertEqual(by_type(st.transactions, "DEPOSIT")[0]["qty"], 100.0)
        self.assertEqual(sorted(t["qty"] for t in by_type(st.transactions, "INTEREST")), [0.05, 10.0])
        self.assertEqual(by_type(st.transactions, "WITHDRAWAL")[0]["qty"], 5.0)
        div, wht = by_type(st.transactions, "DIVIDEND")[0], by_type(st.transactions, "WITHHOLDING_TAX")[0]
        self.assertAlmostEqual(wht["qty"], 0.30 / 1.10)
        self.assertAlmostEqual(div["qty"], 0.70 + 0.30 / 1.10)
        self.assertEqual(st.period_start, datetime(2024, 1, 1))
        self.assertTrue(any("Stock split open" in w for w in st.warnings))


# ---------------------------------------------------------------------------
# eToro
# ---------------------------------------------------------------------------

class EtoroTests(TempDirCase):
    def test_statement(self):
        st = etoro.parse(build_etoro(self.path("et.xlsx")))
        self.assertEqual((st.holder, st.account), ("Petr  Svoboda", "petr1"))
        self.assertEqual((st.period_start, st.period_end), (datetime(2026, 4, 1), datetime(2026, 4, 29, 23, 59, 59)))
        txs = st.transactions
        self.assertEqual([t["qty"] for t in by_type(txs, "DEPOSIT")], [170.0])
        rate = 170.0 / (197.5 + 2.5)                       # EUR / (USD credited + FX fee)
        self.assertEqual(sorted(round(t["qty"], 6) for t in by_type(txs, "FEE")),
                         sorted([round(2.5 * rate, 6), round(1.0 * rate, 6)]))
        aapl = [t for t in txs if t["ticker"] == "AAPL"]
        buy, sell = by_type(aapl, "BUY")[0], by_type(aapl, "SELL")[0]
        self.assertAlmostEqual(buy["price"], 200 * rate)
        self.assertAlmostEqual(sell["qty"] * (sell["price"] - buy["price"]), 8.5)   # cash == EUR profit
        self.assertEqual(buy["uid"], "etoro:petr1:pos:100:buy")
        tsla = [t for t in txs if t["ticker"] == "TSLA"]
        self.assertTrue(all(t["date"] == datetime(2026, 4, 21, 14) for t in tsla))   # short: legs at close
        self.assertAlmostEqual(by_type(tsla, "SELL")[0]["realized_pnl"], -4.25)
        msft_buy = [t for t in by_type(txs, "BUY") if t["ticker"] == "MSFT"]
        self.assertEqual(len(msft_buy), 1)                  # still-open position from activity
        self.assertAlmostEqual(msft_buy[0]["price"], 40 * rate / 0.1)

    def test_dividends_not_double_counted_and_gross(self):
        txs = etoro.parse(build_etoro(self.path("et.xlsx"))).transactions
        divs, whts = by_type(txs, "DIVIDEND"), by_type(txs, "WITHHOLDING_TAX")
        self.assertEqual(len(divs), 1)
        self.assertAlmostEqual(divs[0]["qty"], 0.425 + 0.0765)
        self.assertAlmostEqual(whts[0]["qty"], 0.0765)

    def test_activity_dividends_used_without_dividend_sheet(self):
        txs = etoro.parse(build_etoro(self.path("et.xlsx"), with_dividend_sheet=False)).transactions
        self.assertEqual(len(by_type(txs, "DIVIDEND")), 1)
        self.assertEqual(by_type(txs, "DIVIDEND")[0]["ticker"], "MSFT")

    def test_ticker_extraction(self):
        self.assertEqual(etoro.extract_ticker("Taiwan Semiconductor Manufacturing Co Ltd - ADR (TSM)"), "TSM")
        self.assertEqual(etoro.extract_ticker("Buy NVDA/USD"), "NVDA")
        self.assertEqual(etoro.extract_ticker("Sell Tesla (TSLA)"), "TSLA")
        self.assertEqual(etoro.extract_ticker("BTC/USD"), "BTC")
        with self.assertRaises(ValueError):
            etoro.parse_etoro_date("someday")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class LoaderTests(TempDirCase):
    def test_multiset_dedup(self):
        div = t212_row(Action="Dividend (Dividend)", Time="2024-03-01 10:00:00", Ticker="AAPL",
                       **{"No. of shares": "1", "Total": "0.50", "Currency (Total)": "EUR"})
        build_t212(self.path("Trading212-from_2024-01-01_to_2024-06-30_A.csv"), [div, div])
        build_t212(self.path("Trading212-from_2024-03-01_to_2024-12-31_B.csv"), [div])
        res = load_statements(self.dir)
        self.assertEqual(len(by_type(res.transactions, "DIVIDEND")), 2)

    def test_snapshot_rule_and_coverage_gap(self):
        p1 = ("1", "AAA.US", "Aaa", 1.0, 10.0, 0.0, datetime(2025, 1, 5))
        p2 = ("2", "BBB.US", "Bbb", 1.0, 20.0, 0.0, datetime(2025, 1, 6))
        build_xtb_new(self.path("EUR_a.xlsx"), "42", datetime(2025, 1, 1), datetime(2025, 1, 31), positions=[p1, p2])
        build_xtb_new(self.path("EUR_b.xlsx"), "42", datetime(2025, 6, 1), datetime(2025, 6, 30), positions=[p1])
        res = load_statements(self.dir)
        buys = by_type(res.transactions, "BUY")
        self.assertEqual([t["ticker"] for t in buys], ["AAA"])
        msgs = [w["message"] for w in res.warnings]
        self.assertTrue(any("BBB #2" in m and "2025-01-31" in m and "2025-06-30" in m for m in msgs))
        self.assertTrue(any("no statement covers 2025-01-31 → 2025-06-01" in m for m in msgs))

    def test_closed_snapshot_position_is_not_reported_stale(self):
        p2 = ("2", "BBB.US", "Bbb", 1.0, 20.0, 0.0, datetime(2025, 1, 6))
        build_xtb_new(self.path("EUR_a.xlsx"), "42", datetime(2025, 1, 1), datetime(2025, 1, 31), positions=[p2])
        build_xtb_new(self.path("EUR_b.xlsx"), "42", datetime(2025, 2, 1), datetime(2025, 2, 28), closed=[[
            "Bbb", "BBB.US", "STOCK", "BUY", 1.0, 1.0, datetime(2025, 1, 6), 1.0, datetime(2025, 2, 10),
            "", 1.0, 1.0, 20.0, 21.0, "", "", 0.0, "", 0.0, 0.0, "", "", "", "2", ""]])
        res = load_statements(self.dir)
        self.assertEqual(len(by_type(res.transactions, "BUY")), 1)       # the closed leg only
        self.assertFalse(any("older snapshot" in w["message"] for w in res.warnings))
        self.assertFalse(any("no statement covers" in w["message"] for w in res.warnings))

    def test_holder_warning_and_errors(self):
        build_xtb_old(self.path("xtb.xlsx"), holder="Jan  Novák")
        build_bitpanda(self.path("bitpanda-trades-2026-01-01-10-00.csv"),
                       [bp_row("D1", "2024-01-01T10:00:00+01:00", "deposit", "incoming", "1.00", "-", "EUR", cls="Fiat")],
                       holder="jan novak")
        open(self.path("old.xls"), "wb").close()
        open(self.path("statement.pdf"), "wb").close()
        open(self.path("notes.txt"), "w").close()
        res = load_statements(self.dir)
        self.assertFalse(any("different account holders" in w["message"] for w in res.warnings))
        self.assertEqual(sorted(e["file"] for e in res.errors), ["old.xls", "statement.pdf"])
        self.assertEqual(res.source_counts, {"XTB": 1, "Bitpanda": 1, "eToro": 0, "Trading212": 0})

        build_etoro(self.path("et.xlsx"))
        res = load_statements(self.dir)
        holder_msgs = [w["message"] for w in res.warnings if "different account holders" in w["message"]]
        self.assertEqual(len(holder_msgs), 1)
        self.assertIn("et.xlsx", holder_msgs[0])
        self.assertTrue(res.to_dict()["transactions"][0]["date"].startswith("20"))


if __name__ == "__main__":
    unittest.main()
