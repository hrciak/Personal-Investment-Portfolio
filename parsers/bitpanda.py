"""
Bitpanda transaction-history parser (csv)
=========================================

Bitpanda exports the full account history as CSV with a short preamble
(disclaimer, "Name, birthdate", e-mail, "Account opened at: ...", venue)
followed by the header row starting with "Transaction ID". Timestamps carry a
UTC offset; "-" marks an empty cell.

Mapping (EUR amounts come from "Amount Fiat", i.e. what was actually paid or
received including spread; "Asset market price" can differ from it by tens of
percent on some rows and is not used for trades):

  deposit/withdrawal (Fiat)     -> DEPOSIT / WITHDRAWAL (+ FEE row for EUR fees)
  deposit/withdrawal (crypto)   -> TRANSFER_IN / TRANSFER_OUT at fair value;
                                   a fee paid in the asset itself reduces the
                                   units received (or adds to the units sent)
  buy / sell                    -> BUY / SELL, price = Amount Fiat / Amount Asset,
                                   fee = Fee when charged in EUR
  reward                        -> REWARD (staking income, in-kind)
  transfer, transfer(stake),    -> TRANSFER_IN / TRANSFER_OUT (provisional)
  transfer(unstake)

Bitpanda records every move between its own wallets (spot <-> staking) as an
outgoing + incoming pair with the same asset and amount, about a second apart.
The pairs can straddle two exports, so pairing runs AFTER the loader merged all
files (`postprocess`): matched pairs are dropped, and unmatched stake/unstake
legs are internal by definition and dropped too.

Unmatched plain "transfer" rows are NOT coins the user moved in: Bitpanda books
deposits from an outside wallet as "deposit" rows. In real exports the
unmatched incoming transfers are airdrops, BEST rewards, launchpad tokens and
staking dust credited by Bitpanda, so they are booked as REWARD (in-kind income
at fair value). Unmatched outgoing transfers are kept as TRANSFER_OUT (units
left the account at fair value).
"""

import csv
import io
import os
import re
from datetime import datetime

from parsers.base import (Statement, create_transaction, is_empty, safe_float,
                          to_naive_utc)

BROKER = "Bitpanda"
_HEADER_MARK = "Transaction ID"
_PAIR_WINDOW_S = 5
_PAIR_REL_TOL = 1e-8
# Private key (stripped by the loader) carrying the raw Bitpanda transfer kind
KIND_KEY = "_bitpanda_kind"


def _read_lines(filepath: str) -> list:
    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
        return f.readlines()


def detect(filepath: str) -> bool:
    if not filepath.lower().endswith(".csv"):
        return False
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
    except OSError:
        return False
    return _HEADER_MARK in sample and "Amount Fiat" in sample and "Amount Asset" in sample


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------

_FILENAME_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})")


def _metadata(st: Statement, preamble: list, filepath: str):
    for raw in preamble:
        line = raw.strip().strip('"').strip()
        if line.lower().startswith("account opened at:"):
            st.period_start = to_naive_utc(line.split(":", 1)[1].strip())
        elif st.holder is None and re.search(r",\s*\d{4}-\d{2}-\d{2}$", line):
            st.holder = re.sub(r",\s*\d{4}-\d{2}-\d{2}$", "", line).strip()
    m = _FILENAME_STAMP.search(os.path.basename(filepath))
    if m:
        st.generated_at = datetime(*map(int, m.groups()))


# --------------------------------------------------------------------------
# Row mapping
# --------------------------------------------------------------------------

def _num(row, col) -> float:
    return safe_float(row.get(col))


def _unit_price(fiat: float, units: float) -> float:
    return fiat / units if units > 0 else 0.0


def _row_to_txs(row: dict) -> tuple:
    """Return (transactions, recognised) for one CSV row."""
    tx_id = (row.get("Transaction ID") or "").strip()
    raw_type = (row.get("Transaction Type") or "").strip().lower()
    direction = (row.get("In/Out") or "").strip().lower()
    asset = (row.get("Asset") or "").strip()
    asset_class = (row.get("Asset class") or "").strip()
    when = row.get("Timestamp")
    fiat = abs(_num(row, "Amount Fiat"))
    units = abs(_num(row, "Amount Asset"))
    fee = abs(_num(row, "Fee"))
    fee_asset = (row.get("Fee asset") or "").strip()
    eur_fee = fee if fee_asset.upper() == "EUR" else 0.0
    asset_fee = fee if fee_asset and fee_asset == asset else 0.0
    uid = f"bitpanda:{tx_id}" if tx_id else None
    base = dict(source=BROKER, asset_class_hint=asset_class)
    is_fiat = asset_class.lower() == "fiat"

    def coin(tx_type, qty, **extra):
        return create_transaction(date=when, ticker=asset, tx_type=tx_type, qty=qty,
                                  price=_unit_price(fiat, units), uid=uid, **base, **extra)

    def cash(tx_type, amount, suffix=""):
        return create_transaction(date=when, ticker="CASH", tx_type=tx_type, qty=amount,
                                  price=1.0, source=BROKER, uid=f"{uid}{suffix}" if uid else None)

    if raw_type in ("deposit", "withdrawal"):
        if is_fiat or is_empty(row.get("Amount Asset")):
            txs = [cash("DEPOSIT" if raw_type == "deposit" else "WITHDRAWAL", fiat)]
            if eur_fee > 0:
                txs.append(cash("FEE", eur_fee, ":fee"))
            return txs, True
        # Amount Asset of a crypto deposit is already net of the network fee
        # (subtracting it again leaves later sells short by exactly the fee)
        if raw_type == "deposit":
            return [coin("TRANSFER_IN", units)], True
        return [coin("TRANSFER_OUT", units + asset_fee)], True
    if raw_type in ("buy", "sell"):
        if units <= 0:
            return [], False
        return [coin(raw_type.upper(), units, fee=eur_fee)], True
    if raw_type == "reward":
        return ([coin("REWARD", units)] if units > 0 else []), True
    m = re.fullmatch(r"transfer(?:\((stake|unstake)\))?", raw_type)
    if m:
        if units <= 0:
            return [], True
        tx = coin("TRANSFER_IN" if direction == "incoming" else "TRANSFER_OUT", units)
        tx[KIND_KEY] = m.group(1) or "transfer"
        return [tx], True
    return [], False


def parse(filepath: str) -> Statement:
    st = Statement(broker=BROKER, file=os.path.basename(filepath))
    lines = _read_lines(filepath)
    start = next((i for i, l in enumerate(lines[:20]) if _HEADER_MARK in l), None)
    if start is None:
        raise ValueError("Bitpanda header row ('Transaction ID') not found")
    _metadata(st, lines[:start], filepath)

    unknown = {}
    for row in csv.DictReader(io.StringIO("".join(lines[start:]))):
        if is_empty(row.get("Timestamp")):
            continue
        txs, ok = _row_to_txs(row)
        if not ok:
            key = (row.get("Transaction Type") or "?").strip()
            unknown[key] = unknown.get(key, 0) + 1
        st.transactions.extend(txs)

    if unknown:
        listed = ", ".join(f"{k} ×{n}" for k, n in sorted(unknown.items()))
        st.warnings.append(f"Skipped {sum(unknown.values())} row(s) of unsupported type: {listed}.")
    if st.transactions:
        last = max(t["date"] for t in st.transactions)
        st.period_end = max(filter(None, [st.generated_at, last]))
        st.generated_at = st.generated_at or last
    return st


# --------------------------------------------------------------------------
# Post-merge hook: drop Bitpanda-internal wallet moves
# --------------------------------------------------------------------------

def _qty_match(a: float, b: float) -> bool:
    return abs(a - b) <= max(_PAIR_REL_TOL * max(a, b), 1e-12)


def postprocess(transactions: list, warnings: list) -> list:
    """Pair opposite-direction transfer legs (same asset & amount, <= 5 s apart)
    and drop them; drop unpaired stake/unstake legs; book unpaired incoming
    plain transfers as REWARD and keep unpaired outgoing ones as TRANSFER_OUT."""
    legs = sorted((t for t in transactions if KIND_KEY in t), key=lambda t: t["date"])
    paired = set()
    for i, a in enumerate(legs):
        if id(a) in paired:
            continue
        best = None
        for b in legs[i + 1:]:
            if (b["date"] - a["date"]).total_seconds() > _PAIR_WINDOW_S:
                break
            if (id(b) not in paired and b["type"] != a["type"] and b["ticker"] == a["ticker"]
                    and _qty_match(a["qty"], b["qty"])):
                best = b
                break
        if best is not None:
            paired.update((id(a), id(best)))

    drop = set(paired)
    unpaired_internal = [t for t in legs if id(t) not in paired and t[KIND_KEY] != "transfer"]
    drop.update(id(t) for t in unpaired_internal)
    credits = [t for t in legs if id(t) not in drop and t["type"] == "TRANSFER_IN"]
    outgoing = [t for t in legs if id(t) not in drop and t["type"] == "TRANSFER_OUT"]
    for t in credits:
        t["type"] = "REWARD"

    if legs:
        parts = [f"{len(paired) // 2} internal wallet move(s) (spot <-> staking) removed"]
        if unpaired_internal:
            parts.append(f"{len(unpaired_internal)} unmatched stake/unstake leg(s) removed")
        if credits:
            value = sum(t["qty"] * t["price"] for t in credits)
            parts.append(f"{len(credits)} unmatched incoming transfer(s) (airdrops, BEST rewards, "
                         f"staking dust; €{value:,.2f}) booked as rewards")
        if outgoing:
            parts.append(f"{len(outgoing)} unmatched outgoing transfer(s) booked as in-kind withdrawals")
        warnings.append("Bitpanda: " + "; ".join(parts) + ".")
    return [t for t in transactions if id(t) not in drop]
