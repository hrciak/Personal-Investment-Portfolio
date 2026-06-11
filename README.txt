Personal Investment Portfolio Dashboard
=======================================

A local Flask dashboard that aggregates broker statements from XTB, Bitpanda,
and eToro into a single portfolio view with live prices, P&L, risk metrics,
and charts.


SETUP (first time)
------------------

1. Create a virtual environment:

   Windows:
     py -m venv venv
     venv\Scripts\pip install -r requirements.txt

   Mac / Linux:
     python3 -m venv venv
     venv/bin/pip install -r requirements.txt

2. Drop your broker statement files into the broker-statements/ folder:

   - XTB       → export as .xlsx from the XTB platform
   - Bitpanda  → export as .csv from Bitpanda
   - eToro     → export as .xlsx from eToro (Account Statement)

   You can drop in as many files as you like. Duplicate transactions across
   files are removed automatically, so overlapping date ranges are fine.


RUN
---

   Windows:
     venv\Scripts\python app.py

   Mac / Linux:
     venv/bin/python app.py

Then open http://127.0.0.1:5050 in your browser.

To reload data without restarting, use the Reload button in the dashboard
or POST to http://127.0.0.1:5050/api/reload


SUPPORTED BROKERS
-----------------

  XTB       .xlsx   (export from History > closed/open positions)
  Bitpanda  .csv    (export from Transaction History)
  eToro     .xlsx   (export from Portfolio > Account Statement)
            .csv    (trade history export)

eToro statements are parsed in both English and Czech (sheet/column names are
localized by account language). EUR values from the statement are used directly
where available; USD-only amounts are converted using the EUR/USD rate implied
by the statement's own deposit rows, falling back to a live ECB rate.


UNSUPPORTED FILE FORMATS
------------------------

  .xls   Old Excel 97-2003 format is not readable. Open it in Excel or Google
         Sheets and save as .xlsx.
  .pdf   PDF statements cannot be parsed. Use the broker's .xlsx or .csv export.

Files that cannot be processed are listed in a popup on the dashboard; they are
skipped and do not stop the rest of your statements from loading.


NOTE
----

The broker-statements/ folder is gitignored — your financial files are never
committed to the repository.
