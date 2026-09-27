# Cost Engine — how EX-Work $/KG is computed

This document explains the formula chain implemented in `cost_engine.py`,
which replaces the old flat, hand-typed `product.ex_work_usd_kg` value with
a live computation from editable raw-cost tables. It is a translation of
the source workbook's `Stretch` sheet (and everything that sheet reads
from), reproduced as faithfully as practical in Python/SQLite.

## Where every number lives

| Workbook sheet | New DB table(s) | Admin screen |
|---|---|---|
| Material pricing | `material_rate`, plus several keys in `global_setting` | Material Rates, Global Settings |
| اجور مباشرة (Direct Labor) | `labor_employee` | Labor |
| Electricity | `electricity_power` (kW/ton), `production_capacity` (tons/day) | Electricity |
| تكاليف متغيرة (Variable Costs) | `variable_cost_item`, `global_setting.material_interest_rate` | Variable Costs, Global Settings |
| تكاليف ثابتة (Fixed Costs) | `fixed_cost_item` | Fixed Costs |
| Conversion cost | *(computed, not stored — see below)* | shown on Cost Preview |
| Pallet component | `pallet_component` | Pallet/Packaging |
| BOM | `bom_row` | BOM |
| Stretch (the master sheet) | `cost_engine.compute_ex_work_usd_kg()` | Cost Preview |

## The chain, in order

1. **Material composition (BOM).** Each product's Stretch Ability text
   (e.g. `"300% (Power plus)"`) is parsed for its leading percentage to get
   the BOM sheet's multiplier (1.5 / 2 / 2.5 / 3 / 3.5). Combined with the
   product's Micron and whether its Roll weight is ≤25kg ("standard" BOM
   columns) or >25kg ("jumbo" BOM columns — Stretch!`IF(H3>25,...)`), this
   looks up a `bom_row`: the weight fraction of Exceed 3518, Exceed 3812,
   Exceed XP, Vista 6000, Enable, LD 258 and Vista 6202. C4's fraction is
   never stored — it's always `1 − sum(the others)`, exactly like the
   workbook.
2. **Material cost.** For each material, `fraction × plastic_weight_kg ×
   rate($/ton) / 1000 × 1.01 × 1.031`, summed. `plastic_weight_kg = roll
   weight − core weight` (Stretch column J). The `1.01`/`1.031` multipliers
   are carried over unchanged from the workbook's Stretch!T3:AB3 formulas;
   the workbook has no comment naming them, so they are labelled
   "waste factor" and "scrap/handling factor" (editable under Global
   Settings) — this is an interpretation, not a documented fact from the
   sheet.
3. **Core cost.** `core_weight_kg × (Core rate EGP/kg ÷ Dollar rate)`.
4. **Packaging/pallet cost.** The `pallet_component` row matching the
   product's Auto/Manual and Pallet-size fields is summed (pallet + cardboard
   + cap + corrugated + stretch-wrap + box + cartoon-angle + scotch-tape, each
   × its material rate ÷ dollar rate) and divided by Rolls/Pallet. For jumbo
   rolls (>25kg) using Automatic packing, the base "Pallet" line item is
   excluded before dividing — this mirrors Stretch!AD40's
   `('Pallet component'!D11-'Pallet component'!D6)/G40`, which the standard
   (≤25kg) row (AD3) does not do.
5. **Material interest.** `material_cost × material_interest_rate`. The
   workbook currently has this line disabled for the live month (a note by
   the material-interest cell says it was removed "due to quality of
   material-interest file management … covered by credit interest"), so the
   seeded rate defaults to **0** — but the computed rate (≈1.66%, from
   `((3.216%+5.35%)/4)×(1-22.5%)`) is preserved as the field's documented
   default so an admin can switch it back on.
6. **Conversion cost ("Other costs").** This is the figure the Stretch
   sheet's `AF79:AF85` comment names: **"Depreciation + D.labor + Machine
   Power (24kw/ton)"**. It is computed, not stored, every time it's needed:
   - `kW/ton` for the product's micron/roll-type bucket (`electricity_power`)
     × the variable electricity tariff (EGP/kWh) → variable electricity
     cost/ton, plus the small `variable_cost_item` add-ons.
   - Total `fixed_cost_item` (production + selling + admin + financial) ÷
     monthly production tons (`tons_per_day × Capacity Usage % × 30`) →
     fixed cost/ton.
   - `(variable + fixed) / Dollar Rate` → USD/ton.
   - `× plastic_weight_kg / 1000`, with a narrow-web surcharge
     `× (500 / width_mm)` when the product's Width is set and <500mm
     (Stretch!AF3's `IF(F3<500, …*(500/F3))`).
   - The Direct Labor roster (`labor_employee`) is not consulted directly
     here — its 2024-base and overtime totals are kept in sync into two
     specific `fixed_cost_item` rows ("اجور عمال الانتاج" /
     "اضافي عمال الانتاج") whenever the roster is edited, exactly matching
     how the workbook's تكاليف ثابتة sheet pulls `='اجور مباشرة'!E20` /
     `=L1*'اجور مباشرة'!G20`.
7. **EX-Work Cost (KG)** = `(material + core + packaging + interest + other)
   ÷ roll_weight_kg` — Stretch!AG.
8. **Margin & final price** — unchanged from Phase 1: the Factors table's
   country-class × customer-class × product-category percentage is added on
   top (`pricing.unit_price_for`), then the per-user foreign-seller
   adjustment.

## Roll-type bucket

The Electricity/Conversion-cost sheets only distinguish four machine/roll
buckets: **St** (Standard), **P** (Power), **P_plus** (Power+), **RIGID**.
`cost_engine.roll_type_bucket()` maps a product's free-text Stretch Ability
onto one of these (looking for "regid"/"rigid", "power plus"/"power+",
"power", else Standard). UVI products are **not** a separate electricity
bucket in the workbook — UVI is a coating applied on top of one of the four
physical buckets — so a UVI product is bucketed by its underlying base type.
The margin-factor side (Factors table, unchanged from Phase 1) still has its
own, separate UVI categories for pricing purposes.

## Known approximations / open questions

- **Pre-Stretch rows are now implemented as their own product family**
  (see the dedicated section below) — this replaces the earlier
  approximation that priced pre-stretch SKUs like any other product.
- **UVI weight %** is a manual entry in the workbook's Stretch sheet (not
  BOM-driven) and there's currently no per-product field for it in this
  app, so UVI resin cost contributes $0 until such a field is added.
- **ROUNDUP vs round**: the workbook's Electricity sheet uses `ROUNDUP(...,0)`
  for EGP/ton figures; this app uses ordinary rounding. The difference is at
  most a few EGP/ton (well under a cent per kg).
- **1.01 / 1.031 multipliers**: carried over as literal constants (see step
  2 above) since the workbook itself has no comment explaining them.
- **Capacity Usage %** (80%) is a single global assumption in the workbook
  (`Conversion cost!I2`), applied uniformly to every micron/roll-type; this
  app keeps it that way (one editable global setting) rather than making it
  per-product.
- Values computed here were spot-checked against the original flat
  `ex_work_usd_kg` values imported in Phase 1 and land within roughly 5–10%
  for every sampled product — differences are explained by the disabled
  material-interest rate, the ROUNDUP vs round difference, and the fact that
  the original Phase-1 values were themselves already a specific month's
  snapshot of a workbook whose fixed-cost pool changes every month.

## The Pallet-component manual override

`Pallet component!C28` — the "Small Box: 1.5kg" boxes-per-pallet quantity —
carries this cell comment (translated from Arabic):

> **Hend:** Based on Abdel Karim's revision (phone call in the presence of
> Eng. Rami) — instead of 80.

I.e. the box count for that specific pallet configuration was manually
raised from an original 80 to the current 112, per a verbal approval. This
value is preserved exactly as data (`pallet_component.box_qty = 112` for
`packing_key='manual_smallbox15_usd'`) and the note itself is stored in
`pallet_component.override_note` and shown as a warning/tooltip on the
Pallet/Packaging admin screen next to that field, so it is never silently
edited away without whoever changes it seeing why the number is what it is.

## Packing tiers (exact gross-weight stuffing)

`packing_tier` (admin screen "Packing Tiers") holds the `Details` sheet's
rolls/box, box/pallet, rolls/pallet and pallets/container(40'/20') for every
roll-weight bucket (Manual 5kg / 2.3~3.5kg / 2.2kg / 1.5kg, Automatic Jumbo
50kg / 55kg, Standard 16kg) x pallet type (Standard/Euro). This is separate
from `pallet_component` (which only prices the packaging *materials* per
Automatic/Manual x USD/EUR pallet, with no weight-tier breakdown) — the two
tables answer different questions and neither duplicates the other.

`cost_engine.lookup_packing_tier()` matches a product to a tier by its
Auto/Manual field, the *nearest* `match_weight_kg` to the product's actual
`roll_weight_kg`, and the quote line's chosen pallet type (Standard/Euro).
`effective_rolls_per_pallet()` uses the matched tier's `rolls_per_pallet`
(falling back to the product's own manually-entered `rolls_per_pallet` only
if no tier matches at all), and both the packaging-cost step and the quote
builder's line totals use this instead of a single hardcoded per-product
value. Since every seeded product in this app's catalogue is currently a
16kg Automatic Standard roll, this mostly changes behaviour once products
with other roll weights are added — but the standard vs Euro pallet split
is already live (a Euro-pallet line uses 45 rolls/pallet vs 46 for Standard,
per the workbook).

**Open question:** the `Details` sheet lists *two* different rows for
"Standard Roll (16 kg)" under the Euro-pallet Automatic section (45 vs 30
rolls/pallet) with no label distinguishing when each applies. The 45
rolls/pallet variant was kept as the seeded default (`standard_16kg_euro`);
flagging this for the team to confirm which one is correct, or whether both
are needed as separate tiers.

## Missing jumbo SKUs (v7)

Stretch rows 27, 29, 31, 35, 40, 49 — 50kg jumbo, 16 rolls/pallet, Automatic/
Standard-pallet variants of the existing 250% Power / 300% (Power plus) /
350% (Power plus) categories at micron 17/20/23/30 — were never seeded in
Phase 1 (only the 16kg standard rows were). They are seeded idempotently
by `db._seed_missing_products()` (matched on stretch_ability + micron +
roll_weight_kg, so re-running the app against an already-deployed DB never
duplicates them) and price with the *exact same* cost-engine formula as
every other product — the jumbo BOM rows for multiplier 2.5/3/3.5 at
micron 17/20/23/30 were already seeded, so no BOM/electricity data was
missing for them.

**Row 32 ("250% Power", 23µm, jumbo) is a verbatim duplicate of row 31**
(identical stretch ability, micron, pallet/auto/color/width/roll-weight/
core-weight) — apparently a copy/paste artifact in the source workbook.
Only row 31 was added as a product; row 32 was not seeded a second time.
Flagging this for the owner to confirm whether row 32 was meant to be a
distinct SKU (e.g. a different color or spec that just wasn't filled in) —
if so, the corresponding fields still need to come from somewhere, since
the workbook itself has no second data point to distinguish it.

## Pre-Stretch (v7)

Pre-Stretch (Stretch rows 79–85, micron 5/6/7/8/9/10/12) is a distinct
product family, not just another product row, because it is made to order:
roll weight, core weight and rolls/pallet are typed in by the rep on each
quotation line (`quotation_line.prestretch_roll_weight_kg` /
`prestretch_core_weight_kg` / `prestretch_rolls_per_pallet`), not fixed
catalog values — matching Stretch!H79/I79/G79 being blank in the template.
`product.is_prestretch=1` marks the seven Pre-Stretch catalog rows (their
own `roll_weight_kg`/`core_weight_kg`/`rolls_per_pallet` are NULL, since
those are per-order), and each carries `product.prestretch_source_product_id`
pointing at the jumbo SKU whose finished sales price feeds its material
cost. `pricing.compute_prestretch_line()` is the Pre-Stretch counterpart of
`compute_line()`, called from `/api/calculate-line` and `/api/save-quotation`
whenever `pricing.is_prestretch(product)` is true.

**Micron → source SKU mapping** (editable on the admin **Pre-Stretch**
screen), from Stretch!T79:T85's `=AI<source row>*J<this row>`:

| Pre-Stretch micron | Source SKU (jumbo, 50kg) | Stretch rows |
|---|---|---|
| 5  | 350% (Power plus), 17µm | 79 ← 49 |
| 6  | 300% (Power plus), 17µm | 80 ← 40 |
| 7  | 250% Power, 17µm        | 81 ← 27 |
| 8  | 250% Power, 20µm        | 82 ← 29 |
| 9  | 250% Power, 23µm        | 83 ← 31 |
| 10 | 250% Power, 23µm (same as micron 9 — row 32 is the row-31 duplicate) | 84 ← 32 |
| 12 | 250% Power, 30µm        | 85 ← 35 |

**Formula chain** (`pricing.prestretch_cost_components()` /
`prestretch_packaging_cost_usd()` / `prestretch_ex_work_usd_kg()`), for a
line with entered roll weight H, core weight I, rolls/pallet G and
packaging type (No Boxes / With Boxes):

1. **Net weight** `J = H - I`.
2. **Material cost `T`** = the source SKU's *current finished, margin-
   inclusive sales price* per KG (`pricing.unit_price_for()` against the
   same country/customer classification as the quote — not the raw EX-Work
   cost) `× J`.
3. **Core cost `AC`** = `I × (Core-prestretch rate EGP/kg ÷ Dollar Rate –
   Prestretch)` — `material_rate.core_prestretch` (seeded 30 EGP/kg) and
   `global_setting.dollar_rate_prestretch` (seeded 45, kept as its own
   setting since the workbook formula reads `'Material pricing'!F2`, a
   distinct cell from the main Dollar Rate `F1`, even though both are
   currently 45).
4. **Packaging cost `AD`**: `No Boxes` → `global_setting.
   prestretch_packaging_noboxes_usd` (seeded $14.80, from `'Pallet
   component'!Q11`) `÷ G`; `With Boxes` → `global_setting.
   prestretch_packaging_boxes_usd` (seeded $14.38, from `'Pallet
   component'!V11`) `÷ G`, **plus** 1/6 of a Box's material cost
   (`material_rate.box` ÷ `dollar_rate_prestretch` ÷ 6 — the workbook's
   `'Material pricing'!C21/F2/6` term).
5. **Conversion cost `AF`** ("Depreciation + D.labor + Machine Power"),
   reusing `cost_engine.conversion_cost_usd_per_ton()` exactly as every
   other product does. Bucket: Pre-Stretch's Stretch-Ability text contains
   none of `roll_type_bucket()`'s power/regid keywords, so it already
   resolves to **Standard ("St")** — kept as the deliberate choice (Pre-
   Stretch is a converting/rewinding step off the Standard-bucket line, not
   its own extrusion process). Looked up by the Pre-Stretch SKU's own
   micron (5/6/7/8/9/10/12); microns 5/6/7 have no direct Electricity-sheet
   data point in the St bucket, so the existing nearest-micron fallback
   lands on micron 8/9's row, which happens to be **0 kW/ton and 0 tons/day**
   in the source workbook — i.e. the conversion-cost contribution is $0 for
   those three microns specifically. This is a known gap, not a bug: the
   source Electricity/Production-capacity sheets simply have no data for
   micron 5–7 St. Flagging for the owner: if real kW/ton and tons/day
   figures exist for hand-stretch at those microns, add them on the
   Electricity/Global Settings admin screens and this fills in
   automatically (no code change needed).
   `× J` (no width factor — Pre-Stretch has no Width field).
6. **EX-Work $/KG** = `(T + AC + AD + AF) / H`.
7. **Margin/factor**: applied exactly as for any other product
   (`unit_price = ex_work × (1 + factor) + price_adjustment`). The Factors
   sheet has **no dedicated Pre-Stretch row/column**, so `product_category()`
   falls through to `automatic_standard` (Pre-Stretch's text contains
   neither "power" nor "regid" nor "uvi") — flagging this for the owner to
   confirm the intended Pre-Stretch margin; it may deserve its own Factors
   category and percentage.

**Sanity-check behaviour**: at small, realistic rolls/pallet counts (e.g.
30–50 for a ~1.5kg hand roll) Pre-Stretch prices out notably above its
source jumbo SKU's own $/KG, as expected — packaging cost per roll
dominates. At a very generous rolls/pallet (e.g. 200, unrealistically light
for a small roll), the packaging/core cost gets diluted enough that the
Pre-Stretch price can land close to or slightly under the source's own
price; it is never negative or near-zero for any plausible input, but reps
should enter a realistic rolls/pallet count for the quote to be meaningful.

## Caching

`product.ex_work_usd_kg` remains a column on `product`, but it is now a
**cache**, not the source of truth. `pricing.unit_price_for()` and
`compute_line()` always call `cost_engine.compute_ex_work_usd_kg()` live, so
quotes are always priced from current raw-cost data. The cached column is
only refreshed (for display on the Products admin list) when an admin clicks
"Recalculate EX-Work cost from cost engine" on that page — this was chosen
over recomputing on every raw-cost save because a single rate edit (e.g. one
resin price) affects every product, and updating 100+ product rows on every
keystroke would be needless write load for a value nothing reads
authoritatively.
