#!/usr/bin/env python3
"""
Portfolio performance report (PIPD)
===================================

Loads the broker statements, runs the same engine as the dashboard, samples
its daily series at month-ends, prints an audit table and writes a
self-contained interactive chart to portfolio_performance.html (gitignored).

Every number here comes from engine.compute_portfolio, so the report always
agrees with the dashboard:
  * portfolio value      — daily mark-to-market valuation in EUR
  * net contributions    — deposits − withdrawals (+ in-kind transfers at FMV)
  * TWR index            — flow-neutral daily time-weighted return, base 100
  * benchmark            — S&P 500 total return converted to EUR, base 100
  * MWR                  — XIRR on external cash flows

Run with the project venv:  python build_performance.py
"""

import os
import sys
import json
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from parsers import load_statements
from engine import compute_portfolio

HERE = os.path.dirname(os.path.abspath(__file__))
TX_DIR = os.path.join(HERE, "broker-statements")
OUT_PATH = os.path.join(HERE, "portfolio_performance.html")


def month_end_indices(dates):
    """Index of the last available day of every month (today for the current one)."""
    last = {}
    for i, ds in enumerate(dates):
        last[ds[:7]] = i
    return [last[k] for k in sorted(last)]


def print_sources(loaded):
    print("=" * 64)
    print("DATA SOURCES")
    print("=" * 64)
    for f in loaded.files:
        print(f"  - {f['file']}  [{f.get('broker')}, {f.get('transactions', 0)} tx kept]")
    for e in loaded.errors:
        print(f"  ! {e['file']}: {e['error']}")
    print(f"\n{len(loaded.transactions)} transactions after merging overlapping statements.")


def print_notes(notes):
    if not notes:
        return
    print("\nData-quality notes:")
    for w in notes:
        prefix = f"{w['file']}: " if w.get("file") else ""
        print(f"  * {prefix}{w['message']}")


def build_report_data(portfolio):
    ch, k = portfolio["charts"], portfolio["kpis"]
    dates = ch["dates"]
    me = month_end_indices(dates)
    monthly_dates = [dates[i] for i in me]

    def pick(series, nd=2):
        return [None if series[i] is None else round(series[i], nd) for i in me]

    value, invested = pick(ch["net_worth"]), pick(ch["invested"])
    twr, sp500 = pick(ch["twr"]), pick(ch["benchmark"])
    mirror = pick(ch.get("benchmark_value") or [None] * len(dates))

    # Cash-flow markers: net change in contributions within each month
    markers_abs, markers_twr, marker_info = [], [], []
    prev = 0.0
    for n, i in enumerate(me):
        flow = ch["invested"][i] - prev
        prev = ch["invested"][i]
        if abs(flow) > 0.005:
            markers_abs.append(value[n])
            markers_twr.append(twr[n])
            marker_info.append({"amount": round(abs(flow), 2),
                                "type": "deposit" if flow > 0 else "withdrawal"})
        else:
            markers_abs.append(None)
            markers_twr.append(None)
            marker_info.append(None)

    m5 = sp500[-1] if sp500 else None
    metrics = {
        "m1": round(k["total_value"], 2),
        "m2": round(k["net_contributions"], 2),
        "m3": round(k["total_value"] - k["net_contributions"], 2),
        "m4": twr[-1] if twr else None,
        "m5": m5,
        "m6": round(twr[-1] - m5, 2) if (twr and m5 is not None) else None,
        "m7": round(k["xirr"], 2) if k["xirr"] is not None else None,
    }
    return {
        "labels": [datetime.strptime(d, "%Y-%m-%d").strftime("%b %Y") for d in monthly_dates],
        "rawDates": monthly_dates,
        "portfolioValue": value,
        "investedCapital": invested,
        "twr": twr,
        "sp500": sp500,
        "mirror": mirror,
        "comparison": portfolio.get("benchmark") or {},
        "markersAbs": markers_abs,
        "markersTwr": markers_twr,
        "markerInfo": marker_info,
        "metrics": metrics,
        "spWarn": None if any(v is not None for v in sp500) else "S&P 500 data unavailable",
        "benchmarkLabel": portfolio["meta"].get("benchmark") or "S&P 500",
    }


def print_audit(data):
    print("\n" + "=" * 64)
    print("MONTH-END SNAPSHOTS")
    print("=" * 64)
    hdr = f"{'date':<12}{'value':>14}{'net contrib':>14}{'TWR':>10}{'S&P500':>10}"
    print(hdr)
    print("-" * len(hdr))

    def fmt(v, width, spec):
        return f"{v:>{width}{spec}}" if v is not None else f"{'n/a':>{width}}"

    def row(i):
        print(f"{data['rawDates'][i]:<12}{fmt(data['portfolioValue'][i], 14, ',.0f')}"
              f"{fmt(data['investedCapital'][i], 14, ',.0f')}{fmt(data['twr'][i], 10, '.1f')}"
              f"{fmt(data['sp500'][i], 10, '.1f')}")

    n = len(data["rawDates"])
    head = list(range(min(3, n)))
    tail = list(range(max(3, n - 3), n))
    for i in head:
        row(i)
    if n > 6:
        print("...")
    for i in tail:
        row(i)

    m = data["metrics"]
    print("\nSummary:")
    print(f"  Portfolio value            : EUR {m['m1']:,.2f}")
    print(f"  Net contributions          : EUR {m['m2']:,.2f}")
    print(f"  Absolute gain              : EUR {m['m3']:,.2f}")
    if m["m4"] is not None:
        print(f"  Portfolio TWR (index)      : {m['m4']:.1f}  ({m['m4'] - 100:+.1f}%)")
    print("  Benchmark index            : " + (f"{m['m5']:.1f}" if m["m5"] is not None else "n/a")
          + f"  ({data['benchmarkLabel']})")
    print("  Alpha vs benchmark         : " + (f"{m['m6']:+.1f} pp" if m["m6"] is not None else "n/a"))
    print("  MWR / XIRR (annualised)    : " + (f"{m['m7']:.2f}%" if m["m7"] is not None else "n/a"))

    c = data["comparison"]

    def pct(v):
        return f"{v:.2f}%" if v is not None else "n/a"

    def pp(v):
        return f"{v:+.2f} pp" if v is not None else "n/a"

    print(f"\nvs S&P 500 ({c.get('start')} – {c.get('end')}):")
    if c.get("mirror_value") is not None:
        print(f"  Same deposits in S&P 500   : EUR {c['mirror_value']:,.2f}  "
              f"(you are {'ahead' if c['value_difference'] >= 0 else 'behind'} by EUR {abs(c['value_difference']):,.2f})")
    print(f"  Money-weighted (XIRR)      : you {pct(m['m7'])} vs S&P {pct(c.get('mirror_xirr'))}"
          f"  → {pp(c.get('xirr_difference'))}")
    twr_pa = None if c.get("cagr") is None or c.get("twr_difference") is None else c["cagr"] + c["twr_difference"]
    print(f"  Time-weighted (TWR vs CAGR): you {pct(twr_pa)} p.a. vs S&P {pct(c.get('cagr'))} p.a."
          f"  → {pp(c.get('twr_difference'))}")


def render_html(data):
    # "</" would let a value close the <script> element early
    payload = json.dumps(data).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("/*__DATA__*/", payload)


def main():
    loaded = load_statements(TX_DIR)
    print_sources(loaded)
    if not loaded.transactions:
        print("No transactions found — nothing to report.")
        return
    portfolio = compute_portfolio(loaded.transactions)
    print_notes(list(loaded.warnings) + list(portfolio["warnings"]))

    data = build_report_data(portfolio)
    print_audit(data)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(render_html(data))
    print(f"\nSaved: {OUT_PATH}")


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
    c3 = {lbl:'Portfolio TWR', val:m.m4==null?'n/a':idx1(m.m4), cls:(m.m4||0)>=100?'pos':'neg'};
    c4 = {lbl:'Alpha vs. S&P 500', val:m.m6==null?'n/a':pp(m.m6), cls:(m.m6||0)>=0?'pos':'neg'};
  }
  const cards = [
    {lbl:'Portfolio value', val:eur(m.m1), cls:''},
    {lbl:'Net contributions', val:eur(m.m2), cls:''},
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
      ['swatch dash','color:#888780','Net contributions (EUR)'],
      ['swatch dash','color:#EF9F27','Same deposits in S&P 500 (EUR)'],
      ['swatch dot','background:#E24B4A','Cash flow event'],
    ];
  }
  return [
    ['swatch','background:#1D9E75','Portfolio TWR (base 100)'],
    ['swatch dash','color:#EF9F27',DATA.benchmarkLabel+' (base 100)'],
    ['swatch dot','background:#E24B4A','Cash flow event (TWR-neutral)'],
  ];
}

function renderLegend(){
  document.getElementById('legend').innerHTML = legendHTML().map(([cls,style,txt]) =>
    `<div class="item"><span class="${cls}" style="${style}"></span>${txt}</div>`
  ).join('');
  document.getElementById('modeNote').textContent = mode === 'abs'
    ? 'Absolute view: real EUR balances. Net contributions are cumulative deposits − withdrawals (a step function). The amber line is what the same deposits and withdrawals would be worth had they gone into the S&P 500 (total return, EUR) on the same days — a like-for-like comparison.'
    : 'Performance view: Time-Weighted Return removes the effect of deposit/withdrawal size and timing, making it directly comparable to the S&P 500 (in EUR). Both indexed to 100 at inception. Cash flow markers sit on the line but do not move the return series.';
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
      {label:'Net contributions', data:DATA.investedCapital, borderColor:'#888780', backgroundColor:'#888780',
       borderWidth:1.5, borderDash:[5,3], pointRadius:0, tension:0, yAxisID:'y'},
      {label:'Same deposits in S&P 500', data:DATA.mirror, borderColor:'#EF9F27', backgroundColor:'#EF9F27',
       borderWidth:1.5, borderDash:[4,2], pointRadius:0, tension:0.3, yAxisID:'y'},
      {label:'Cash flow', data:showEvents?DATA.markersAbs:[], parsing:true, showLine:false,
       pointRadius:5, pointHoverRadius:7, pointBackgroundColor:'#E24B4A', pointBorderColor:'#fff',
       pointBorderWidth:1.5, yAxisID:'y'},
    ];
  }
  return [
    {label:'Portfolio TWR', data:DATA.twr, borderColor:'#1D9E75', backgroundColor:'#1D9E75',
     borderWidth:2, pointRadius:0, tension:0.3, yAxisID:'y'},
    {label:'S&P 500 (EUR)', data:DATA.sp500, borderColor:'#EF9F27', backgroundColor:'#EF9F27',
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
  } else {
    base.y = {position:'left', grid:{color:gridColor},
      title:{display:true,text:'Performance index (base 100 = inception)',color:axisColor,font:{size:11}},
      ticks:{color:axisColor,font:{size:11}, callback:v=>Math.round(v)}};
  }
  return base;
}

function tooltipLabel(ctx){
  const ds = ctx.dataset.label;
  if(ctx.raw == null) return null;
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
