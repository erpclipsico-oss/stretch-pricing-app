# Stretch Pricing App (Phase 1)

A working quote-builder web app modeled on your existing Stretch Pricing tool:
login with roles, a quotation screen (product / pallet / packing / discount),
saved quotation history, and PDF export.

## What this phase includes

- **Login + roles**: `admin` and `sales_rep`. Admins see/manage every quotation
  and can create users; sales reps see only their own quotations.
- **Quote builder**: loading port, destination, payment term, customer &
  country classification, global discount %, and multiple product lines
  (product, pallet type, packing type, quantity in pallets).
- **Pricing engine**: unit price = the product's EX-Work $/KG rate (imported
  from your `Stretch_Export_Pricing_H1.24_August_2024.xlsx` → `Report0` sheet)
  **times** the margin factor for the selected Country Classification x
  Customer Classification x product category (imported from the `Factors`
  sheet). 19 priced product variants were imported (the rows in your sheet
  that had non-zero prices).
- **Save + history + PDF export** of quotations.

### Known simplification (by design, for this phase)

Your spreadsheet's full cost chain (labor, electricity, raw-material cost,
conversion cost, BOM per spec) feeds into the EX-Work price that's already
calculated in `Report0`. This phase **imports that already-computed price**
rather than rebuilding the whole cost engine, so it can't yet answer "what
happens to price if resin cost goes up." Freight rates per destination are
shown for reference (from your `Details` sheet) but aren't yet folded
automatically into the quoted unit price.

**Phase 2** (not built yet) would add: admin screens to edit the underlying
cost tables (materials, labor, electricity, BOM) so prices recompute
automatically, and automatic freight/landed-cost proration into the quote.

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Visit http://localhost:5000 — default accounts:

| Username     | Password       | Role      |
|--------------|----------------|-----------|
| admin        | ChangeMe123!   | admin     |
| sales_rep1   | ChangeMe123!   | sales_rep |

**Change these passwords immediately** — go to the Users page as admin to add
real accounts, then remove or disable these defaults directly in the database
(there's no "change password" screen yet; add one, or edit via the admin
users table, in phase 2).

## Deploying (Render, like the original Stretch app)

1. Push this folder to a GitHub repo.
2. In Render, "New +" → "Blueprint", point it at the repo — `render.yaml` is
   already set up (free web service + a 1GB persistent disk for the SQLite
   database, since Render's filesystem is otherwise wiped on every deploy).
3. Render will install `requirements.txt` and start the app with `gunicorn`.
4. First boot seeds the database automatically from `app/data/seed_data.json`
   (the extracted rate tables) and creates the two default accounts above.

If you'd rather deploy to Railway, Fly.io, or your own server: any host that
runs a Python web app + gives you persistent disk works the same way — just
make sure `DB_PATH` points somewhere that survives restarts/redeploys.

## Project structure

```
app/
  app.py          Flask routes (auth, quote builder, PDF export, admin)
  db.py           SQLite schema + seeding (no external DB server needed)
  pricing.py      The pricing calculation (base rate x margin factor)
  data/seed_data.json   Rate tables extracted from your spreadsheet
  templates/      HTML pages
  static/style.css
run.py            App entry point
requirements.txt
Procfile          For gunicorn on Render/Heroku-style hosts
render.yaml       One-click Render blueprint (web service + disk)
```

## Re-importing updated rates

When your spreadsheet's prices/factors change, re-run the extraction: open
`app/data/seed_data.json` and update the `products` / `factors` / `freight`
arrays to match (or ask me to re-extract from a new spreadsheet export — I
can regenerate this file for you). Note the app only seeds this data once
(on first run with an empty database) — to force a refresh, delete the
`product`, `factor`, and `freight` rows (or the whole `pricing.db` file, if
you don't need to keep saved quotations) and restart.
