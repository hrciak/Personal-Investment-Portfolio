Personal Investment Portfolio Dashboard
=======================================

A local Flask dashboard that merges broker statements from XTB, Bitpanda,
eToro and Trading 212 into one EUR portfolio view with live prices, P&L,
time-weighted and money-weighted returns, risk metrics and charts.


SETUP (first time)
------------------

1. Create a virtual environment:

   Windows:
     py -m venv venv
     venv\Scripts\pip install -r requirements.txt

   Mac / Linux:
     python3 -m venv venv
     venv/bin/pip install -r requirements.txt

2. Drop your broker statement files into the broker-statements/ folder
   (see SUPPORTED BROKERS). You can drop in as many files as you like:
   overlapping exports are merged automatically (see MERGING).


RUN
---

   Windows:      venv\Scripts\python app.py
   Mac / Linux:  venv/bin/python app.py

Then open http://127.0.0.1:5050 in your browser.

Results are computed once and cached. After adding or changing statements,
press "Reload Engine" in the dashboard (or POST to /api/reload). JSON of the
full result is available at /api/data.

Environment variables: PIPD_PORT (default 5050), PIPD_DEBUG=1 (Flask debug
mode with auto-reload — local use only).


SUPPORTED BROKERS
-----------------

  XTB         .xlsx  account statement — both the older export
                     (CLOSED POSITION HISTORY / OPEN POSITION / CASH OPERATION
                     HISTORY sheets) and the newer one (Closed Positions /
                     Open Positions / Cash Operations)
  Bitpanda    .csv   Transaction History export
  eToro       .xlsx  Account Statement (English or Czech); .csv trade history
  Trading 212 .csv   History export

All amounts are converted to EUR. XTB trades use the statement's EUR
purchase/sale values, Trading 212 its EUR totals, eToro the EUR figures in the
statement (USD-only amounts use the statement's own EUR/USD deposit rate).


MERGING
-------

Each transaction gets a stable broker id where the broker provides one
(XTB position/operation ids, Bitpanda transaction ids, Trading 212 ids), so
overlapping or repeated exports never double count. For the same id, the most
recent export wins. For brokers whose exports are snapshots (XTB open
positions), only the newest snapshot defines what is currently held.

The dashboard lists data-quality notes, for example:
  * a period no statement covers (download the missing range from the broker),
  * a position that was open in an older export but closed in such a gap,
  * a negative reconstructed cash balance (deposits missing from the files),
  * statements that belong to different account holders.


HOW VALUES ARE CALCULATED
-------------------------

  * Prices: daily Yahoo Finance closes converted to EUR (crypto: <COIN>-EUR,
    CoinGecko fallback). Every price feed is checked against the prices you
    actually traded at; a feed that does not match (a different instrument
    with the same ticker, or the wrong unit) is rejected and the holding is
    valued at its last statement price. See "Price Sources" in the dashboard.
  * Total P&L = realized trades + unrealized + dividends + interest + staking
    rewards − withholding tax − other fees.
  * TWR (time-weighted return) removes the effect of deposit/withdrawal size
    and timing; volatility, Sharpe, Sortino, drawdown and beta use the same
    flow-neutral daily returns. The benchmark is the S&P 500 total return
    in EUR.
  * XIRR (money-weighted return) uses only deposits, withdrawals and in-kind
    transfers, plus today's value.
  * "vs S&P 500" compares like with like, over your portfolio's own period:
      - money-weighted: your XIRR vs the XIRR of a mirror portfolio that put
        every deposit/withdrawal into the S&P 500 (total return, EUR) on the
        same day — plus what that mirror would be worth today. Comparing XIRR
        with the index's plain CAGR would be skewed by when you added money.
      - time-weighted: your TWR p.a. vs the index's CAGR.

Wrong symbol? Create symbol_overrides.json in the project root:

    {"RHM": "RHM.DE", "Bitpanda:BEST": null}

Keys are TICKER or Broker:TICKER; null disables the feed for that holding.


COMMAND-LINE TOOLS
------------------

  python build_performance.py   monthly audit table + portfolio_performance.html
  python diagnostics.py         Markdown/JSON report of every processing issue
                                (--no-probe skips the exchange-suffix probe)
  python find_anomalies.py      price-feed validation table

All three use the same loader and engine as the dashboard.


TESTS
-----

  python -m unittest discover -s tests

The tests use synthetic data and a fake market (no network).


UNSUPPORTED FILE FORMATS
------------------------

  .xls   Old Excel 97-2003 format is not readable. Open it in Excel or Google
         Sheets and save as .xlsx.
  .pdf   PDF statements cannot be parsed. Use the broker's .xlsx or .csv export.

Files that cannot be processed are listed in a popup on the dashboard; they
are skipped and do not stop the rest of your statements from loading.


NOTE
----

broker-statements/, generated reports, symbol_cache.json and
symbol_overrides.json are gitignored — your financial data is never committed.
