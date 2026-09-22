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

- **Pre-stretch rows** (Stretch rows 79–85, the ones the AF79:AF85 comment
  is actually attached to) are costed very differently in the workbook:
  their material cost (`T79`) is `=AI49*J79` — i.e. it borrows another row's
  *finished sales price* as an input, rather than being built up from BOM +
  conversion cost independently. This app's product catalogue does not
  currently model that cross-row dependency; pre-stretch SKUs will price
  using the same general formula as every other product (their own BOM/
  Pallet/Conversion-cost inputs), which is a reasonable approximation but
  is **not** a byte-for-byte match to the workbook for that specific
  product family. Flagging this for the team to confirm.
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
