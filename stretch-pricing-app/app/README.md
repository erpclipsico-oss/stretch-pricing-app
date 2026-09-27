# Stretch Pricing App (Phase 2: full cost engine)

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

### Phase 2: the full cost engine is now live

EX-Work $/KG is no longer a flat, hand-typed number. It's computed live from
every underlying cost sheet in your spreadsheet, all of it editable from
admin screens:

- **Material Rates** — resin prices ($/ton: C4, Exceed 3518/3812/XP, Vista
  6000, Enable, UVI, LD, Vista) and packaging rates (EGP: Core, Stretch wrap,
  Cardboard, Cap, Pallet, Box, Cartoon angle, Scotch tape).
- **Labor** — the full direct-labor roster (اجور مباشرة): name, role, 2023
  base wage, and increase rate. 2024 wages and the overtime pool are computed
  automatically and feed straight into Fixed Costs.
- **Electricity** — machine power (kW/ton) and production capacity
  (tons/day) per micron and roll type (Standard/Power/Power+/RIGID).
- **Variable Costs** and **Fixed Costs** (تكاليف متغيرة / تكاليف ثابتة) —
  every line item from both sheets (production, selling, admin, financial
  overhead), editable, with the grand total spread across production
  capacity to become the "Conversion Cost" (Depreciation + Direct Labor +
  Machine Power) baked into every product's price.
- **Pallet/Packaging** — every packaging configuration (Automatic/Manual ×
  USD/EUR pallet) with its item quantities, including a manual override on
  the "Small Box 1.5kg" configuration that's called out with the original
  approval note (a phone-call sign-off) right in the admin screen.
- **BOM** — material composition % per stretch-ability/micron/roll-weight
  tier, driving each product's resin mix.
- **Global Settings** — dollar rate, capacity usage %, and the handful of
  other constants everything else depends on.
- **Cost Preview** — a read-only, line-by-line breakdown of every product's
  EX-Work cost right now, to sanity-check any change before it reaches a
  quote.

See `app/COST_ENGINE.md` for the full write-up of how these combine — useful
if your team wants to audit or extend the logic later. It also documents a
couple of places where the spreadsheet's intent had to be interpreted (noted
there as open questions), and the pre-stretch product family's known
approximation.

Freight rates per destination are still shown for reference only and aren't
yet folded automatically into the quoted unit price — a good candidate for a
future phase.

### Phase 3 additions

- **Packing Tiers (exact gross-weight stuffing).** A new admin screen
  ("Packing Tiers") holds rolls/box, box/pallet, rolls/pallet and
  pallets/container (40'/20') for every roll-weight bucket from the
  workbook's `Details` sheet — Manual 5kg / 2.3~3.5kg / 2.2kg / 1.5kg, and
  Automatic Jumbo 50kg / 55kg / Standard 16kg — each split by Standard vs
  Euro pallet. Every quote line's packaging cost and rolls-per-pallet now
  come from matching the product's actual roll weight (and the line's
  chosen pallet type) to the closest tier here, instead of one fixed value
  typed per product.
- **Gross / Net / Per-KG pricing basis, per quote line.** Each line has its
  own dropdown so a rep can quote one customer's line by full roll weight
  (Gross), one by plastic-only weight (Net, i.e. roll weight minus the
  core), and another simply per KG — all in the same quotation. The chosen
  basis shows on-screen and on the PDF ("$/Roll (Gross)", "$/Roll (Net)",
  "$/KG").
- **Factors shown as whole percentages.** The Factors admin screen now
  takes/shows "15" to mean 15%, instead of "0.15" — no change to how the
  numbers are stored or used in pricing, just the on-screen input.
- **Local vs Foreign seller, made explicit.** Every user account now has a
  visible Seller Type (Local/Foreign) on the Users admin screen, and a
  logged-in foreign-seller account sees a small badge at the top of the
  quote builder ("🌍 Foreign Seller — +$0.03/KG applied"). Two new foreign
  accounts were added: `pasquale` and `manuel`.
- **Annual Cost Upload.** A new "Annual Cost Upload" admin screen accepts
  next year's pricing workbook (same sheet layout), shows a full line-by-line
  before/after review (old value → new value, % change) with nothing written
  to the database yet, and only applies the changes once you click "Apply".
  Every upload is logged (date, who, filename, what changed) for an audit
  trail. Currently covers Material Rates, Global Settings (Dollar Rate etc.),
  Labor wages, Variable Costs and Fixed Costs — the Electricity table, BOM,
  Pallet component and Packing Tiers are not yet part of the automated diff
  (still edited by hand on their own screens) since they change far less
  often; see `app/COST_ENGINE.md` / `app/cost_upload.py` if you want that
  extended later.

### Phase 4 (v7) additions: missing jumbo SKUs + Pre-Stretch

- **Six missing jumbo SKUs added.** The `Stretch` sheet had seven "jumbo
  pre-stretch precursor" rows (50kg jumbo, 16 rolls/pallet, same 250%/300%/
  350% categories as existing products) that were never seeded in Phase 1:
  250% Power @ 17/20/23/30µm, 300% (Power plus) @ 17µm, 350% (Power plus) @
  17µm. A seventh row (250% Power, 23µm) was a verbatim duplicate of the
  23µm row already listed and was **not** added a second time — see
  `app/COST_ENGINE.md` for the detail. These price exactly like any other
  product; no new logic was needed.
- **Pre-Stretch (hand-stretch film), a new product family.** Seven SKUs
  (micron 5/6/7/8/9/10/12) are now selectable in the quote builder. Unlike
  every other product, Pre-Stretch is made-to-order: the rep types in **roll
  weight, core weight, rolls/pallet and a packaging type (No Boxes / With
  Boxes)** per quotation line instead of using fixed catalog values —
  selecting a Pre-Stretch product reveals these four extra fields under the
  line. Its material cost is not built from the BOM independently: each
  Pre-Stretch micron borrows its *source* jumbo SKU's current finished sales
  $/KG and multiplies it by the line's entered net weight. See
  `app/COST_ENGINE.md` for the full formula and the micron → source mapping,
  editable on the new **Pre-Stretch** admin screen (also holds the two fixed
  packaging totals, alongside Global Settings).
- **Three categories flagged, not built.** `Special (Power Plus)`, `UVI
  Film` (Standard/Power/Power+) and `(UV&REGID) Film` have zero cost/spec
  data anywhere in the source workbook (no roll weight, core weight, width
  or rolls/pallet for any micron) — nothing was fabricated for them. They
  are **not** in the product catalog; the owner needs to supply real specs
  before they can be added via the normal Products admin screen.

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Visit http://localhost:5000 — default accounts:

| Username     | Password       | Role      | Seller type |
|--------------|----------------|-----------|-------------|
| admin        | ChangeMe123!   | admin     | local       |
| sales_rep1   | ChangeMe123!   | sales_rep | local       |
| pasquale     | ChangeMe123!   | sales_rep | foreign (+$0.03/KG) |
| manuel       | ChangeMe123!   | sales_rep | foreign (+$0.03/KG) |

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
