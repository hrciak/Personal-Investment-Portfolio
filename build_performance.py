#!/usr/bin/env python3
"""
Portfolio performance visualization builder (PIPD)
==================================================

Reads the broker statements, computes monthly performance metrics
(Absolute wealth + Time-Weighted Return vs. S&P 500), prints an audit
table, and writes a self-contained interactive chart to
portfolio_performance.html.

Run with the project venv:  python build_performance.py
"""

import os
import sys
import json
from collections import defaultdict
from datetime import datetime, date

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd
import yfinance as yf

try:
    from pyxirr import xirr
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "pyxirr"], check=False)
    from pyxirr import xirr

import app
from engine import compute_portfolio

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# STEP 1 — read data
# ---------------------------------------------------------------------------

def load_transactions():
    txs = app.load_all_transactions()
    # Same signature dedup the live app uses
    seen, deduped = set(), []
    for t in txs:
        dtag = t["date"].isoformat() if hasattr(t["date"], "isoformat") else str(t["date"])
        sig = f'{dtag}|{t["ticker"]}|{t["type"]}|{t["qty"]}|{t["price"]}'
        if sig not in seen:
            seen.add(sig)
            deduped.append(t)
    return deduped


def print_data_summary(deduped):
    print("=" * 64)
    print("STEP 1 — DATA SOURCES")
    print("=" * 64)
    files = [f for f in os.listdir(app.TX_DIR) if not f.startswith(".")]
    print(f"broker-statements/ contains {len(files)} file(s):")
    for f in files:
        print(f"   - {f}")
    if app.parsing_errors:
        print("Files skipped (unsupported / duplicate):")
        for e in app.parsing_errors:
            print(f"   - {e['file']}: {e['error'][:60]}")
    from collections import Counter
    print(f"\nParsed {len(deduped)} transactions after dedup.")
    print("  by source:", dict(Counter(t["source"] for t in deduped)))
    print("  by type  :", dict(Counter(t["type"] for t in deduped)))
    print("Transaction fields available: date, type, ticker, qty, price, fee, source, currency")
    print("\nNotes / assumptions:")
    print("  * Portfolio valuation is NOT stored; it is derived mark-to-market from")
    print("    positions x historical prices (engine.compute_portfolio).")
    print("  * Month-end = last calendar day present for each month (today for the")
    print("    current month).")
    print("  * S&P 500 (^GSPC, adjusted close, USD) is converted to EUR via EURUSD=X")
    print("    and indexed to 100 at portfolio inception. If FX fetch fails, the USD")
    print("    index is used and a warning is printed.")
    print()


# ---------------------------------------------------------------------------
# STEP 2 — metrics
# ---------------------------------------------------------------------------

def fmt_date(d):
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]


def build_daily(deduped):
    """Reuse the engine for the daily valuation/invested/benchmark series
    (these already-computed metrics are verified and reused), and derive the
    daily net external cash flow separately for a correct TWR."""
    d = compute_portfolio(deduped)
    dates = d["charts"]["dates"]
    nw = [float(x) for x in d["charts"]["net_worth"]]
    inv = [float(x) for x in d["charts"]["invested"]]

    netcf = defaultdict(float)          # date -> signed EUR (deposit +, withdrawal -)
    events = []                          # (date, signed_amount, type)
    for t in deduped:
        amt = float(t["qty"]) * float(t["price"])
        ds = fmt_date(t["date"])
        if t["type"] == "DEPOSIT":
            netcf[ds] += amt
            events.append((ds, amt, "deposit"))
        elif t["type"] == "WITHDRAWAL":
            netcf[ds] -= amt
            events.append((ds, -amt, "withdrawal"))
    return dates, nw, inv, netcf, events


def monthly_twr(monthly_dates, monthly_values, netcf):
    """Time-Weighted Return via month-end sub-period Modified Dietz, indexed
    to 100 at inception and continuous (never rebased).

        R_i = (End_i - Begin_i - CF_i) / (Begin_i + 0.5 * CF_i)
        TWR  = 100 * Π (1 + R_i)

    CF_i (net deposits in the month) is removed from the numerator and half-
    weighted in the denominator, so the index is independent of deposit size
    and timing. Per-month R is clamped to [-0.6, 1.0]: a real diversified month
    never exceeds that, so the clamp only neutralizes valuation glitches (e.g.
    a month whose reconstructed value is understated by missing price history)
    instead of letting one bad point flip the whole chain negative.
    """
    mflow = defaultdict(float)
    for ds, amt in netcf.items():
        mflow[ds[:7]] += amt

    twr = []
    idx = 100.0
    prev = None
    for k, ds in enumerate(monthly_dates):
        end = monthly_values[k]
        cf = mflow.get(ds[:7], 0.0)
        if prev is None:                      # inception month: no prior value
            twr.append(100.0)
        else:
            denom = prev + 0.5 * cf
            r = (end - prev - cf) / denom if denom > 1e-6 else 0.0
            r = max(-0.6, min(1.0, r))
            idx *= (1.0 + r)
            twr.append(idx)
        prev = end
    return twr


def month_end_indices(dates):
    last = {}
    for i, ds in enumerate(dates):
        last[ds[:7]] = i           # later index overwrites -> last day of month
    return [last[k] for k in sorted(last)]


def sp500_eur_indexed(inception, today, monthly_dates):
    """S&P 500 total-return (adjusted close) in EUR, indexed to 100 at inception,
    sampled at the supplied month-end dates."""
    warn = None
    try:
        g = yf.download("^GSPC", start=inception, end=today, auto_adjust=True, progress=False)["Close"]
        if hasattr(g, "columns"):
            g = g.iloc[:, 0]
        g = g.dropna()
        if g.empty:
            raise ValueError("no ^GSPC data")
    except Exception as e:
        print(f"  WARNING: S&P 500 fetch failed ({e}); benchmark will be empty.")
        return [None] * len(monthly_dates), "S&P 500 fetch failed"

    fx = None
    try:
        fx = yf.download("EURUSD=X", start=inception, end=today, auto_adjust=True, progress=False)["Close"]
        if hasattr(fx, "columns"):
            fx = fx.iloc[:, 0]
        fx = fx.dropna()
        if fx.empty:
            fx = None
    except Exception:
        fx = None

    if fx is None:
        warn = "EURUSD fetch failed - S&P shown in USD terms (FX not applied)"
        print(f"  WARNING: {warn}")
        eur = g.copy()
    else:
        idx = g.index.union(fx.index)
        g2 = g.reindex(idx).ffill()
        fx2 = fx.reindex(idx).ffill()
        eur = (g2 / fx2).dropna()          # USD price / (USD per EUR) = EUR price

    base = eur.asof(pd.Timestamp(inception))
    if base is None or pd.isna(base) or base == 0:
        base = float(eur.iloc[0])
    indexed = eur / float(base) * 100.0

    out = []
    for ds in monthly_dates:
        v = indexed.asof(pd.Timestamp(ds))
        out.append(round(float(v), 2) if v is not None and not pd.isna(v) else None)
    return out, warn


def compute_mwr(deduped, final_value):
    flows = []
    for t in deduped:
        amt = float(t["qty"]) * float(t["price"])
        dt = t["date"] if isinstance(t["date"], datetime) else pd.to_datetime(t["date"]).to_pydatetime()
        if t["type"] == "DEPOSIT":
            flows.append((dt, -amt))
        elif t["type"] == "WITHDRAWAL":
            flows.append((dt, amt))
    flows.append((datetime.now(), final_value))
    try:
        rate = xirr(flows)
        return rate * 100.0 if rate is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    deduped = load_transactions()
    print_data_summary(deduped)

    dates, nw, inv, netcf, events = build_daily(deduped)
    if not dates:
        print("No transactions / dates available — aborting.")
        return

    me = month_end_indices(dates)
    monthly_dates = [dates[i] for i in me]

    # Invested capital (spec A2) = cumulative net deposits (deposits - withdrawals),
    # a step function changing only on cash-flow months. (The engine's `inv` series
    # is cost-basis of holdings, which is a different quantity.)
    cum, netdep_daily = 0.0, []
    for ds in dates:
        cum += netcf.get(ds, 0.0)
        netdep_daily.append(cum)

    portfolio_value = [round(nw[i], 2) for i in me]
    invested_capital = [round(netdep_daily[i], 2) for i in me]
    twr_series = monthly_twr(monthly_dates, [nw[i] for i in me], netcf)
    twr_monthly = [round(v, 2) for v in twr_series]

    inception = dates[0]
    today = date.today().isoformat()
    sp500, sp_warn = sp500_eur_indexed(inception, today, monthly_dates)

    # Monthly cash-flow markers (aggregate net flow within each month)
    month_flow = defaultdict(float)
    for ds, amt, typ in events:
        month_flow[ds[:7]] += amt
    markers_abs, markers_twr, marker_info = [], [], []
    for k, ds in enumerate(monthly_dates):
        f = month_flow.get(ds[:7], 0.0)
        if abs(f) > 1e-9:
            markers_abs.append(portfolio_value[k])
            markers_twr.append(twr_monthly[k])
            marker_info.append({"amount": round(abs(f), 2),
                                "type": "deposit" if f > 0 else "withdrawal"})
        else:
            markers_abs.append(None)
            markers_twr.append(None)
            marker_info.append(None)

    # Derived metrics M1-M7
    m1 = round(nw[-1], 2)
    m2 = round(netdep_daily[-1], 2)
    m3 = round(m1 - m2, 2)
    m4 = round(twr_monthly[-1], 2)
    m5 = sp500[-1] if sp500 and sp500[-1] is not None else None
    m6 = round(m4 - m5, 2) if m5 is not None else None
    m7 = compute_mwr(deduped, nw[-1])

    # ----- STEP 4.1 audit table -----
    print("=" * 64)
    print("STEP 2/4 — VERIFICATION TABLE (monthly month-end snapshots)")
    print("=" * 64)
    hdr = f"{'date':<12}{'port_value':>14}{'invested':>14}{'TWR':>10}{'S&P500':>10}"
    print(hdr)
    print("-" * len(hdr))

    def row(i):
        sp = sp500[i]
        sp_s = f"{sp:>10.1f}" if sp is not None else f"{'n/a':>10}"
        print(f"{monthly_dates[i]:<12}{portfolio_value[i]:>14,.0f}"
              f"{invested_capital[i]:>14,.0f}{twr_monthly[i]:>10.1f}{sp_s}")

    n = len(monthly_dates)
    for i in range(min(3, n)):
        row(i)
    if n > 6:
        print(f"{'...':<12}")
    for i in range(max(3, n - 3), n):
        row(i)

    print("\nSummary metrics (M1-M7):")
    print(f"  M1 Portfolio value          : EUR {m1:,.2f}")
    print(f"  M2 Total invested capital   : EUR {m2:,.2f}")
    print(f"  M3 Absolute gain            : EUR {m3:,.2f}")
    print(f"  M4 Portfolio TWR (index)    : {m4:.1f}  ({m4 - 100:+.1f}%)")
    print(f"  M5 S&P 500 (index)          : {m5:.1f}" if m5 is not None else "  M5 S&P 500 (index)          : n/a")
    if m6 is not None:
        print(f"  M6 Alpha vs S&P 500         : {m6:+.1f} pp")
    else:
        print("  M6 Alpha vs S&P 500         : n/a")
    print(f"  M7 MWR / XIRR (annualised)  : {m7:.2f}%" if m7 is not None else "  M7 MWR / XIRR               : n/a")

    # TWR cash-flow-independence sanity check
    print("\nTWR independence check (a deposit must NOT jump the index):")
    biggest_dep = max((e for e in events if e[2] == "deposit"), key=lambda x: x[1], default=None)
    if biggest_dep:
        ds = biggest_dep[0]
        try:
            mi = next(i for i, md in enumerate(monthly_dates) if md[:7] == ds[:7])
            around = [round(x, 1) for x in twr_monthly[max(0, mi - 1):mi + 2]]
            print(f"  Largest deposit EUR {biggest_dep[1]:,.0f} on {ds}; TWR index around it: {around}")
            print("  (the index moves only with market returns, not with the deposit)")
        except StopIteration:
            pass

    # Data-quality caveat surfaced by the audit
    gap_months = sum(1 for k in range(len(monthly_dates))
                     if invested_capital[k] > 0 and portfolio_value[k] < 0.6 * invested_capital[k])
    if gap_months:
        print(f"\n  CAVEAT: {gap_months} month(s) show portfolio value far below invested capital.")
        print("  The engine reconstructs history from currently-open positions only, so")
        print("  positions opened-and-later-sold (and tickers lacking price history) are")
        print("  undervalued in the past. MWR/XIRR and current absolute figures are exact;")
        print("  TWR/Alpha are directional until historical valuation is completed.")

    # ----- STEP 3/4.2 build + save HTML -----
    data = {
        "labels": [datetime.strptime(d, "%Y-%m-%d").strftime("%b %Y") for d in monthly_dates],
        "rawDates": monthly_dates,
        "portfolioValue": portfolio_value,
        "investedCapital": invested_capital,
        "twr": twr_monthly,
        "sp500": sp500,
        "markersAbs": markers_abs,
        "markersTwr": markers_twr,
        "markerInfo": marker_info,
        "metrics": {"m1": m1, "m2": m2, "m3": m3, "m4": m4,
                    "m5": m5, "m6": m6, "m7": round(m7, 2) if m7 is not None else None},
        "spWarn": sp_warn,
    }
    out_path = os.path.join(HERE, "portfolio_performance.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_html(data))

    print("\n" + "=" * 64)
    print("STEP 4 — OUTPUT")
    print("=" * 64)
    print(f"Saved: {out_path}")


def render_html(data):
    payload = json.dumps(data)
    return HTML_TEMPLATE.replace("/*__DATA__*/", payload)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Portfolio Performance</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0d1117; --panel:#161b27; --border:rgba(255,255,255,.08);
    --t1:#e8eaf0; --t2:#888780; --blue:#378ADD; --teal:#1D9E75;
    --amber:#EF9F27; --red:#E24B4A; --gray:#888780;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--t1);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:28px}
  .wrap{max-width:1080px;margin:0 auto}
  h1{font-size:20px;font-weight:600;margin-bottom:4px}
  .sub{color:var(--t2);font-size:13px;margin-bottom:22px}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
  .card .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--t2);font-weight:600}
  .card .val{font-size:24px;font-weight:700;margin-top:8px;letter-spacing:-.02em}
  .pos{color:var(--teal)} .neg{color:var(--red)}
  .bar{display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap}
  .modes{display:inline-flex;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:3px}
  .modes button{background:transparent;border:none;color:var(--t2);padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit}
  .modes button.active{background:var(--blue);color:#fff}
  .modes button.active.twr{background:var(--teal)}
  .chk{display:inline-flex;align-items:center;gap:8px;color:var(--t2);font-size:13px;cursor:pointer;user-select:none}
  .chk input{width:15px;height:15px;accent-color:var(--red)}
  .chk .dot{width:9px;height:9px;border-radius:50%;background:var(--red)}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px}
  .chart-box{position:relative;height:360px}
  .legend{display:flex;gap:20px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px solid var(--border)}
  .legend .item{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--t1)}
  .legend .swatch{width:18px;height:3px;border-radius:2px}
  .legend .swatch.dash{background:repeating-linear-gradient(90deg,currentColor 0 5px,transparent 5px 8px)}
  .legend .swatch.dot{width:9px;height:9px;border-radius:50%}
  .note{color:var(--t2);font-size:12px;margin-top:12px;line-height:1.5}
</style>
</head>
<body>
<div class="wrap">
  <h1>Portfolio Performance</h1>
  <div class="sub" id="subtitle"></div>

  <div class="cards" id="cards"></div>

  <div class="bar">
    <div class="modes">
      <button id="btnAbs" class="active" onclick="setMode('abs')">Absolute wealth</button>
      <button id="btnTwr" onclick="setMode('twr')">TWR vs. S&amp;P 500</button>
    </div>
    <label class="chk"><input type="checkbox" id="chkEvents" checked onchange="render()"><span class="dot"></span>Cash flow markers</label>
  </div>

  <div class="panel">
    <div class="chart-box"><canvas id="chart"></canvas></div>
    <div class="legend" id="legend"></div>
    <div class="note" id="modeNote"></div>
  </div>
</div>

<script>
const DATA = /*__DATA__*/;
let mode = 'abs';
let chart = null;

const eur = v => '€' + Math.round(v).toLocaleString('en-US');
const idx1 = v => Number(v).toFixed(1);
const pp = v => (v>=0?'+':'') + Number(v).toFixed(1) + ' pp';
const pct = v => Number(v).toFixed(2) + '%';

function renderCards(){
  const m = DATA.metrics;
  let c3, c4;
  if(mode === 'abs'){
    c3 = {lbl:'Absolute gain', val:eur(m.m3), cls:m.m3>=0?'pos':'neg'};
    c4 = {lbl:'MWR (XIRR, ann.)', val:m.m7==null?'n/a':pct(m.m7), cls:(m.m7||0)>=0?'pos':'neg'};
  } else {
    c3 = {lbl:'Portfolio TWR', val:idx1(m.m4), cls:m.m4>=100?'pos':'neg'};
    c4 = {lbl:'Alpha vs. S&P 500', val:m.m6==null?'n/a':pp(m.m6), cls:(m.m6||0)>=0?'pos':'neg'};
  }
  const cards = [
    {lbl:'Portfolio value', val:eur(m.m1), cls:''},
    {lbl:'Invested capital', val:eur(m.m2), cls:''},
    c3, c4
  ];
  document.getElementById('cards').innerHTML = cards.map(c =>
    `<div class="card"><div class="lbl">${c.lbl}</div><div class="val ${c.cls}">${c.val}</div></div>`
  ).join('');
}

function legendHTML(){
  if(mode === 'abs'){
    return [
      ['swatch','background:#378ADD','Portfolio value (EUR)'],
      ['swatch dash','color:#888780','Invested capital (EUR)'],
      ['swatch dash','color:#EF9F27','S&P 500 (base 100, right axis)'],
      ['swatch dot','background:#E24B4A','Cash flow event'],
    ];
  }
  return [
    ['swatch','background:#1D9E75','Portfolio TWR (base 100)'],
    ['swatch dash','color:#EF9F27','S&P 500 (base 100)'],
    ['swatch dot','background:#E24B4A','Cash flow event (TWR-neutral)'],
  ];
}

function renderLegend(){
  document.getElementById('legend').innerHTML = legendHTML().map(([cls,style,txt]) =>
    `<div class="item"><span class="${cls}" style="${style}"></span>${txt}</div>`
  ).join('');
  document.getElementById('modeNote').textContent = mode === 'abs'
    ? 'Absolute view: real EUR balances. Invested capital is cumulative net deposits (a step function). The S&P 500 (right axis, base 100) is a scale reference only — not a like-for-like comparison.'
    : 'Performance view: Time-Weighted Return removes the effect of deposit/withdrawal size and timing, making it directly comparable to the S&P 500. Both indexed to 100 at inception. Cash flow markers sit on the line but do not move the return series.';
}

function setMode(m){
  mode = m;
  document.getElementById('btnAbs').classList.toggle('active', m==='abs');
  document.getElementById('btnTwr').classList.toggle('active', m==='twr');
  document.getElementById('btnTwr').classList.toggle('twr', m==='twr');
  renderCards();
  renderLegend();
  render();
}

const axisColor = '#888780';
const gridColor = 'rgba(255,255,255,0.06)';

function buildDatasets(showEvents){
  if(mode === 'abs'){
    return [
      {label:'Portfolio value', data:DATA.portfolioValue, borderColor:'#378ADD', backgroundColor:'#378ADD',
       borderWidth:2, pointRadius:0, tension:0.3, yAxisID:'y'},
      {label:'Invested capital', data:DATA.investedCapital, borderColor:'#888780', backgroundColor:'#888780',
       borderWidth:1.5, borderDash:[5,3], pointRadius:0, tension:0, yAxisID:'y'},
      {label:'S&P 500 (base 100)', data:DATA.sp500, borderColor:'#EF9F27', backgroundColor:'#EF9F27',
       borderWidth:1.5, borderDash:[4,2], pointRadius:0, tension:0.3, yAxisID:'y1'},
      {label:'Cash flow', data:showEvents?DATA.markersAbs:[], parsing:true, showLine:false,
       pointRadius:5, pointHoverRadius:7, pointBackgroundColor:'#E24B4A', pointBorderColor:'#fff',
       pointBorderWidth:1.5, yAxisID:'y'},
    ];
  }
  return [
    {label:'Portfolio TWR', data:DATA.twr, borderColor:'#1D9E75', backgroundColor:'#1D9E75',
     borderWidth:2, pointRadius:0, tension:0.3, yAxisID:'y'},
    {label:'S&P 500', data:DATA.sp500, borderColor:'#EF9F27', backgroundColor:'#EF9F27',
     borderWidth:1.5, borderDash:[5,3], pointRadius:0, tension:0.3, yAxisID:'y'},
    {label:'Cash flow', data:showEvents?DATA.markersTwr:[], showLine:false,
     pointRadius:5, pointHoverRadius:7, pointBackgroundColor:'#E24B4A', pointBorderColor:'#fff',
     pointBorderWidth:1.5, yAxisID:'y'},
  ];
}

function scales(){
  const base = {
    x:{grid:{color:gridColor}, ticks:{color:axisColor,font:{size:11},maxTicksLimit:12,autoSkip:true}},
  };
  if(mode === 'abs'){
    base.y = {position:'left', grid:{color:gridColor},
      ticks:{color:axisColor,font:{size:11}, callback:v=>'€'+v.toLocaleString('en-US')}};
    base.y1 = {position:'right', grid:{drawOnChartArea:false},
      title:{display:true,text:'S&P 500 (base 100)',color:axisColor,font:{size:11}},
      ticks:{color:axisColor,font:{size:11}}};
  } else {
    base.y = {position:'left', grid:{color:gridColor},
      title:{display:true,text:'Performance index (base 100 = inception)',color:axisColor,font:{size:11}},
      ticks:{color:axisColor,font:{size:11}, callback:v=>Math.round(v)}};
  }
  return base;
}

function tooltipLabel(ctx){
  const ds = ctx.dataset.label;
  if(ds === 'Cash flow'){
    const info = DATA.markerInfo[ctx.dataIndex];
    if(!info) return null;
    const base = `${info.type==='deposit'?'Deposit':'Withdrawal'}: ${eur(info.amount)}`;
    return mode==='twr' ? [base, '(TWR-neutral — not affecting the return series)'] : base;
  }
  if(ds.indexOf('S&P') === 0 || ds === 'Portfolio TWR') return `${ds}: ${idx1(ctx.raw)}`;
  return `${ds}: ${eur(ctx.raw)}`;
}

function render(){
  const showEvents = document.getElementById('chkEvents').checked;
  if(chart) chart.destroy();
  chart = new Chart(document.getElementById('chart'), {
    type:'line',
    data:{labels:DATA.labels, datasets:buildDatasets(showEvents)},
    options:{
      responsive:true, maintainAspectRatio:false,
      interaction:{mode:'index', intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'rgba(13,17,23,.96)', borderColor:'rgba(255,255,255,.12)', borderWidth:1,
          padding:11, cornerRadius:9, titleColor:'#e8eaf0', bodyColor:'#c9ccd6',
          callbacks:{label:tooltipLabel}
        }
      },
      scales:scales()
    }
  });
}

document.getElementById('subtitle').textContent =
  DATA.rawDates.length ? `${DATA.rawDates[0]} → ${DATA.rawDates[DATA.rawDates.length-1]} · ${DATA.rawDates.length} months`
                       : 'No data';
if(DATA.spWarn){ document.getElementById('subtitle').textContent += '  ·  ' + DATA.spWarn; }
renderCards(); renderLegend(); render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
