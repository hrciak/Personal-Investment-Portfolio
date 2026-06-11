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
   - eToro     → export as .xlsx from eToro


RUN
---

   Windows:
     venv\Scripts\python app.py

   Mac / Linux:
     venv/bin/python app.py

Then open http://127.0.0.1:5000 in your browser.

To reload data without restarting, use the Reload button in the dashboard
or POST to http://127.0.0.1:5000/api/reload


SUPPORTED BROKERS
-----------------

  XTB       .xlsx   (export from History > closed/open positions)
  Bitpanda  .csv    (export from Transaction History)
  eToro     .xlsx   (export from Portfolio > Account Statement)


NOTE
----

The broker-statements/ folder is gitignored — your financial files are never
committed to the repository.
