"""
Statement loader: detect, parse and merge every file in a directory
===================================================================

`load_statements(directory)` is the single entry point used by the web app and
the CLI tools. It

  1. detects each file's broker (exactly one parser must claim it),
  2. parses it into a `Statement` (transactions + period / account metadata),
  3. merges overlapping statements per (broker, account):
       * transactions with a broker uid: one per uid, taken from the most
         recent statement (newest export wins when a broker revised a row);
       * transactions without uid: multiset-max by signature - identical rows
         inside one file survive, overlaps across files collapse;
       * open-position snapshot legs: only the newest snapshot counts; older
         snapshot positions that vanished without any close record are dropped
         and reported (they were closed in a period no statement covers);
       * broker post-merge hooks (Bitpanda internal-transfer pairing),
  4. reports data-quality warnings: coverage gaps between statements, files of
     different account holders, and per-file parser notes.

Files that cannot be processed are listed in `errors`; they never stop the
remaining files from loading.
"""

import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from parsers import bitpanda, etoro, trading212, xtb
from parsers.base import BROKERS, Statement

PARSERS = (xtb, etoro, bitpanda, trading212)
_MODULE_BY_BROKER = {m.BROKER: m for m in PARSERS}

SKIP_EXTENSIONS = {".txt", ".md"}
GAP_DAYS = 7
_FILE_KEY = "_file"

# Same-timestamp ordering: inflows first, BUY before SELL (closed shorts are
# booked BUY+SELL at the same instant)
_TYPE_ORDER = {t: i for i, t in enumerate([
    "DEPOSIT", "TRANSFER_IN", "INTEREST", "DIVIDEND", "REWARD", "BUY", "SELL",
    "FEE", "WITHHOLDING_TAX", "TRANSFER_OUT", "WITHDRAWAL"])}

_ERR_XLS = ("Excel 97-2003 (.xls) files can't be read. Open the file in Excel or Google "
            "Sheets and save it as .xlsx.")
_ERR_PDF = ("PDF statements can't be parsed. Export the statement from your broker as "
            ".xlsx or .csv instead.")


@dataclass
class LoadResult:
    transactions: list = field(default_factory=list)
    files: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    source_counts: dict = field(default_factory=lambda: {b: 0 for b in BROKERS})

    def to_dict(self) -> dict:
        def iso(v):
            return v.isoformat() if isinstance(v, datetime) else v
        return {
            "transactions": [{k: iso(v) for k, v in t.items()} for t in self.transactions],
            "files": [{k: iso(v) for k, v in f.items()} for f in self.files],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "source_counts": dict(self.source_counts),
        }


# --------------------------------------------------------------------------
# Per-file parsing
# --------------------------------------------------------------------------

def _parse_file(path: str):
    """Return (Statement, None) or (None, error message)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xls":
        return None, _ERR_XLS
    if ext == ".pdf":
        return None, _ERR_PDF
    if ext not in (".xlsx", ".csv"):
        return None, f"Unsupported file type '{ext or '(none)'}'. Use the broker's .xlsx or .csv export."
    matches = [m for m in PARSERS if m.detect(path)]
    if not matches:
        if ext == ".csv":
            return None, "Unknown CSV format: headers did not match Bitpanda, Trading 212 or eToro exports."
        return None, "Unrecognized .xlsx layout: not an XTB or eToro statement."
    if len(matches) > 1:
        return None, "Ambiguous format (claimed by " + ", ".join(m.BROKER for m in matches) + ")."
    module = matches[0]
    try:
        st = module.parse(path)
    except Exception as e:  # one broken file must not stop the others
        return None, f"{module.BROKER} parser failed: {e}"
    st.file = os.path.basename(path)
    return st, None


def _recency(st: Statement, mtimes: dict) -> datetime:
    return (st.generated_at or st.snapshot_at or st.period_end
            or mtimes.get(st.file) or datetime.min)


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

def _signature(t: dict) -> tuple:
    return (t["source"], t["account"], t["type"], t["ticker"], t["position_id"],
            t["date"].replace(microsecond=0), round(t["qty"], 8), round(t["price"], 6))


def _label(broker: str, account) -> str:
    return f"{broker} account {account}" if account else broker


def _merge_group(broker, account, stmts, mtimes, warnings) -> list:
    stmts = sorted(stmts, key=lambda s: (_recency(s, mtimes), s.file))
    rank = {id(s): i for i, s in enumerate(stmts)}

    # A statement with an open-positions sheet is a snapshot even when that
    # sheet is empty (then nothing from older snapshots is still open)
    snap_stmts = [s for s in stmts if s.snapshot_at is not None
                  or any(t["snapshot"] for t in s.transactions)]
    newest_snap = max(snap_stmts, key=lambda s: (s.snapshot_at or _recency(s, mtimes), rank[id(s)]),
                      default=None)

    by_uid = {}
    best_sig = {}
    older_snapshot = {}
    for s in stmts:
        sig_groups = defaultdict(list)
        for t in s.transactions:
            if t["snapshot"] and s is not newest_snap:
                older_snapshot[t["position_id"] or id(t)] = (t, s)
                continue
            if t["uid"]:
                by_uid[t["uid"]] = t        # stmts are oldest -> newest: newest wins
            else:
                sig_groups[_signature(t)].append(t)
        for sig, group in sig_groups.items():
            if sig not in best_sig or len(group) >= len(best_sig[sig]):
                best_sig[sig] = group
    merged = list(by_uid.values()) + [t for g in best_sig.values() for t in g]

    if newest_snap is not None and older_snapshot:
        _warn_stale(broker, account, newest_snap, older_snapshot, merged, warnings)
    return merged


def _warn_stale(broker, account, newest_snap, older_snapshot, merged, warnings):
    open_now = {t["position_id"] for t in newest_snap.transactions if t["snapshot"]}
    sold = {t["position_id"] for t in merged if t["type"] == "SELL" and t["position_id"]}
    stale = [(t, s) for pid, (t, s) in older_snapshot.items()
             if pid not in open_now and pid not in sold]
    if not stale:
        return
    new_date = (newest_snap.snapshot_at or newest_snap.generated_at)
    listed = ", ".join(
        f"{t['ticker']} #{t['position_id']} (open on {(s.snapshot_at or s.generated_at):%Y-%m-%d})"
        for t, s in sorted(stale, key=lambda x: x[0]["ticker"]))
    warnings.append({"file": None, "message": (
        f"{_label(broker, account)}: {len(stale)} position(s) from an older snapshot are not open in "
        f"the {new_date:%Y-%m-%d} snapshot and have no close record, so they were closed in a period "
        f"not covered by any statement and are excluded: {listed}.")})


def _coverage_gaps(broker, account, stmts, warnings):
    spans = sorted((s.period_start, s.period_end) for s in stmts
                   if s.period_start and s.period_end and s.period_end >= s.period_start)
    if not spans:
        return
    _, cur_end = spans[0]
    for start, end in spans[1:]:
        if start - cur_end > timedelta(days=GAP_DAYS):
            warnings.append({"file": None, "message": (
                f"{_label(broker, account)}: no statement covers {cur_end:%Y-%m-%d} → {start:%Y-%m-%d}; "
                "deposits, trades and dividends in that window are missing. Export that period "
                "to complete the history.")})
        cur_end = max(cur_end, end)


# --------------------------------------------------------------------------
# Account holders
# --------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    stripped = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def _holder_warning(stmts, warnings):
    holders = defaultdict(lambda: {"display": None, "files": []})
    for s in stmts:
        if s.holder:
            h = holders[_norm_name(s.holder)]
            h["display"] = h["display"] or re.sub(r"\s+", " ", s.holder).strip()
            h["files"].append(s.file)
    if len(holders) > 1:
        parts = "; ".join(f"{h['display']}: {', '.join(sorted(h['files']))}" for h in holders.values())
        warnings.append({"file": None, "message": (
            f"Statements belong to {len(holders)} different account holders and are merged into one "
            f"portfolio — remove the files that are not yours. {parts}")})


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _collapse_warnings(warnings: list) -> list:
    """Overlapping exports repeat the same per-file note; show each message once,
    naming the first file and how many others it applies to."""
    order, files = [], {}
    for w in warnings:
        msg = w["message"]
        if msg not in files:
            order.append(msg)
            files[msg] = []
        if w.get("file") and w["file"] not in files[msg]:
            files[msg].append(w["file"])
    out = []
    for msg in order:
        names = files[msg]
        label = None
        if names:
            label = names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1} more)"
        out.append({"file": label, "message": msg})
    return out


def load_statements(directory: str) -> LoadResult:
    result = LoadResult()
    if not os.path.isdir(directory):
        return result

    statements, mtimes = [], {}
    # os.listdir, not glob: the project path contains [ ] (glob pattern syntax)
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        if os.path.splitext(name)[1].lower() in SKIP_EXTENSIONS:
            continue
        st, err = _parse_file(path)
        if err:
            result.errors.append({"file": name, "error": err})
            result.files.append({"file": name, "broker": None, "account": None, "transactions": 0,
                                 "period_start": None, "period_end": None, "status": "error"})
            continue
        mtimes[name] = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).replace(tzinfo=None)
        for t in st.transactions:
            t[_FILE_KEY] = name
        statements.append(st)
        result.source_counts[st.broker] = result.source_counts.get(st.broker, 0) + 1
        result.warnings.extend({"file": name, "message": w} for w in st.warnings)

    groups = defaultdict(list)
    for st in statements:
        groups[(st.broker, st.account)].append(st)
    merged = []
    for (broker, account), stmts in groups.items():
        txs = _merge_group(broker, account, stmts, mtimes, result.warnings)
        hook = getattr(_MODULE_BY_BROKER.get(broker), "postprocess", None)
        if hook:
            notes = []
            txs = hook(txs, notes)
            result.warnings.extend({"file": None, "message": n} for n in notes)
        _coverage_gaps(broker, account, stmts, result.warnings)
        merged.extend(txs)
    _holder_warning(statements, result.warnings)

    kept = defaultdict(int)
    for t in merged:
        kept[t[_FILE_KEY]] += 1
    for st in statements:
        n = kept.get(st.file, 0)
        status = "ok" if n else ("duplicate" if st.transactions else "empty")
        result.files.append({"file": st.file, "broker": st.broker, "account": st.account,
                             "transactions": n, "parsed": len(st.transactions),
                             "period_start": st.period_start, "period_end": st.period_end,
                             "status": status})
    result.files.sort(key=lambda f: f["file"])
    result.warnings = _collapse_warnings(result.warnings)

    result.transactions = sorted(
        ({k: v for k, v in t.items() if not k.startswith("_")} for t in merged),
        key=lambda t: (t["date"], _TYPE_ORDER.get(t["type"], 99)))
    return result
