"""Pricing engine.

Phase 1 approach: start from the EX-Work $/KG base rate already computed in
the source spreadsheet's Report0 sheet for each product, then apply the
country-classification x customer-classification x product-category margin
factor from the Factors sheet to get the quoted unit price.

Phase 2 (future) replaces the imported base rate with a full recompute from
raw cost inputs (labor, electricity, resin/material cost, conversion cost,
BOM) so the whole chain is editable, not just the final margin step.
"""


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


def unit_price_for(db, product, country_class, customer_class, roll_size="standard"):
    category = product_category(product)
    row = get_factor_row(db, country_class, customer_class, roll_size)
    factor = (row[category] if row is not None else 0.0) or 0.0
    return round(product["ex_work_usd_kg"] * (1 + factor), 4)


def compute_line(db, product, country_class, customer_class, quantity_pallets, roll_size="standard"):
    unit_price = unit_price_for(db, product, country_class, customer_class, roll_size)
    rolls_per_pallet = product["rolls_per_pallet"] or 0
    roll_weight = product["roll_weight_kg"] or 0
    total_kg = round((quantity_pallets or 0) * rolls_per_pallet * roll_weight, 3)
    return unit_price, total_kg
