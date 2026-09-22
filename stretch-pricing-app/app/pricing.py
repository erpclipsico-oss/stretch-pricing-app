"""Pricing engine.

Phase 1 started from a flat, hand-typed EX-Work $/KG rate per product
(imported from the source spreadsheet's already-computed Report0 sheet),
then applied the country-classification x customer-classification x
product-category margin factor from the Factors sheet to get the quoted
unit price.

Phase 2 (this version) replaces the flat rate with a live recompute from
the raw cost inputs (labor, electricity, resin/material cost, conversion
cost, BOM, pallet/packaging) via cost_engine.compute_ex_work_usd_kg(), so
every input up the chain is editable and the EX-Work cost always reflects
current rates. The margin-factor step below is unchanged from Phase 1.
See app/COST_ENGINE.md for the full formula chain.
"""

from . import cost_engine


def product_label(product):
    return f"{product['micron']}μm – {product['stretch_ability']}"


def product_category(product):
    s = (product["stretch_ability"] or "").lower()
    uv = "uvi" in s
    if "uv" in s and "regid" in s:
        return "uv_regid"
    if "regid" in s:
        return "regid"
    if "power plus" in s or "power+" in s or "300%" in s or "350%" in s or "special" in s:
        base = "power_plus"
    elif "power" in s:
        base = "power"
    else:
        base = "standard"
    return ("uvi_" + base) if uv else ("automatic_" + base)


def get_factor_row(db, country_class, customer_class, roll_size="standard"):
    return db.execute(
        "SELECT * FROM factor WHERE country_class=? AND customer_class=? AND roll_size=?",
        (country_class, customer_class, roll_size),
    ).fetchone()


def unit_price_for(db, product, country_class, customer_class, roll_size="standard", price_adjustment_usd_kg=0,
                    pallet_type=None):
    category = product_category(product)
    row = get_factor_row(db, country_class, customer_class, roll_size)
    factor = (row[category] if row is not None else 0.0) or 0.0
    ex_work = cost_engine.compute_ex_work_usd_kg(db, product, pallet_type=pallet_type)
    base = ex_work * (1 + factor)
    return round(base + (price_adjustment_usd_kg or 0), 4)


def compute_line(db, product, country_class, customer_class, quantity_pallets, roll_size="standard",
                  price_adjustment_usd_kg=0, pallet_type=None, pricing_basis="per_kg"):
    """Returns (unit_price_usd_kg, total_kg).

    pricing_basis controls which roll weight the line's total KG (and
    therefore its $ total) is built from:
      - 'per_kg' / 'gross': total KG uses the full (gross) roll weight,
        i.e. what the app has always done -- the $/KG rate applied to the
        entire physical roll including its core.
      - 'net': total KG uses the *plastic* roll weight (gross - core), so
        the line is priced/quoted on the net (saleable plastic) weight only.
    The underlying $/KG cost-engine rate is identical either way; only the
    weight the rate is multiplied by changes. This is a judgement call
    confirmed with the business owner: "gross" and "per KG" are the same
    total-weight basis, just displayed differently ($/Roll vs $/KG) on the
    quote/PDF.
    """
    rolls_per_pallet = cost_engine.effective_rolls_per_pallet(db, product, pallet_type)
    unit_price = unit_price_for(db, product, country_class, customer_class, roll_size, price_adjustment_usd_kg,
                                 pallet_type=pallet_type)
    gross_roll_weight = product["roll_weight_kg"] or 0
    net_roll_weight = max(gross_roll_weight - (product["core_weight_kg"] or 0), 0)
    roll_weight = net_roll_weight if pricing_basis == "net" else gross_roll_weight
    total_kg = round((quantity_pallets or 0) * rolls_per_pallet * roll_weight, 3)
    return unit_price, total_kg
