"""Pricing engine.

Phase 1 started from a flat, hand-typed EX-Work $/KG rate per product
(imported from the source spreadsheet's already-computed Report0 sheet),
then applied the country-classification x customer-classification x
product-category margin factor from the Factors sheet to get the quoted
unit price.

Phase 2 replaced the flat rate with a live recompute from the raw cost
inputs (labor, electricity, resin/material cost, conversion cost, BOM,
pallet/packaging) via cost_engine.compute_ex_work_usd_kg(), so every input
up the chain is editable and the EX-Work cost always reflects current
rates.

Phase 3 (this version, v18) replaces the country_class x customer_class x
roll_size margin-factor step with a micron x film_type x packing_type x
roll_size lookup (cost_engine.margin_pct_for()), matching the reference
app ("Hesham Natora"'s stretch-pricing-app) the owner asked this to fully
replace -- not run alongside -- the old classification-based system.
country_class/customer_class are still accepted as parameters on
unit_price_for()/compute_line() for call-site compatibility but are no
longer used for margin.
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


def unit_price_for(db, product, country_class, customer_class, roll_size="standard", price_adjustment_usd_kg=0,
                    pallet_type=None, rolls_per_pallet_override=None, seller_type=None, apply_extras=True):
    """country_class / customer_class are no longer used for margin (v18 --
    fully replaced by cost_engine.margin_pct_for()'s micron x film_type x
    packing_type x roll_size lookup, per the owner's explicit instruction to
    replace rather than run the two systems side by side). Kept as accepted
    parameters -- rather than removed -- purely so every existing call site
    in app.py/pricing.py doesn't need to change its call shape; they're
    simply ignored here now. Same for `roll_size`, which the old `factor`
    lookup used but every call site already always passed "standard" for.

    seller_type: the priced-for user's `user.seller_type` ('foreign' or
    'local'/None) -- drives the Extras "Foreign sellers extra %" markup.
    apply_extras: set False for an internal lookup of another product's own
    price (e.g. Pre-Stretch borrowing its source SKU's sales price) so the
    Extras surcharges aren't silently double-applied; every top-level quote
    line leaves this at the default True."""
    factor = cost_engine.margin_pct_for(db, product, pallet_type=pallet_type,
                                         rolls_per_pallet_override=rolls_per_pallet_override)
    ex_work = cost_engine.compute_ex_work_usd_kg(db, product, pallet_type=pallet_type,
                                                  rolls_per_pallet_override=rolls_per_pallet_override)
    base = ex_work * (1 + factor)
    if apply_extras:
        base += cost_engine.color_extra_usd_kg(db, product)
    price = base + (price_adjustment_usd_kg or 0)
    if apply_extras:
        price *= cost_engine.foreign_seller_extra_multiplier(db, seller_type)
    return round(price, 4)


def compute_line(db, product, country_class, customer_class, quantity_pallets, roll_size="standard",
                  price_adjustment_usd_kg=0, pallet_type=None, pricing_basis="per_kg",
                  roll_weight_kg=None, core_weight_kg=None, width_mm=None, rolls_per_pallet_override=None,
                  seller_type=None):
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

    roll_weight_kg / core_weight_kg / width_mm / rolls_per_pallet_override:
    per-quotation-line overrides of the product catalog's defaults, since
    the actual roll weight, core weight, width and (for non-standard
    weights) rolls/pallet vary by customer order for EVERY product, not
    just Pre-Stretch (see cost_engine.with_overrides()). None means "use
    the catalog value" for each.
    """
    effective = cost_engine.with_overrides(product, roll_weight_kg, core_weight_kg, width_mm)
    rolls_per_pallet = cost_engine.effective_rolls_per_pallet(db, effective, pallet_type, rolls_per_pallet_override)
    unit_price = unit_price_for(db, effective, country_class, customer_class, roll_size, price_adjustment_usd_kg,
                                 pallet_type=pallet_type, rolls_per_pallet_override=rolls_per_pallet_override,
                                 seller_type=seller_type)
    gross_roll_weight = effective["roll_weight_kg"] or 0
    net_roll_weight = max(gross_roll_weight - (effective["core_weight_kg"] or 0), 0)
    roll_weight = net_roll_weight if pricing_basis == "net" else gross_roll_weight
    total_kg = round((quantity_pallets or 0) * rolls_per_pallet * roll_weight, 3)
    return unit_price, total_kg


# ---------------------------------------------------------------------
# Pre-Stretch (Stretch rows 79-85): unlike every other product, this
# family is made-to-order -- roll weight, core weight and rolls/pallet are
# not fixed catalog values but are typed in per quotation line -- and its
# material cost is not built from the BOM independently: it borrows its
# "source" jumbo SKU's own finished, margin-inclusive sales price ($/KG)
# and multiplies by this line's entered net weight (Stretch!T79=AI49*J79).
# See COST_ENGINE.md for the full formula chain and the seeded micron ->
# source-product mapping (product.prestretch_source_product_id).
# ---------------------------------------------------------------------

def is_prestretch(product):
    return bool(product["is_prestretch"]) if "is_prestretch" in product.keys() else False


def prestretch_cost_components(db, product, roll_weight_kg, core_weight_kg, country_class, customer_class):
    """The three of Stretch!AG79's four addends that don't depend on
    Rolls/Pallet: T79 (material, from the source SKU's sales price),
    AC79 (core) and AF79 (conversion cost). Returns
    (material_cost, core_cost, other_costs). The fourth addend, AD79
    (packaging), depends on Rolls/Pallet too and is computed separately by
    prestretch_packaging_cost_usd()."""
    roll_weight = roll_weight_kg or 0
    core_weight = core_weight_kg or 0
    net_weight = max(roll_weight - core_weight, 0)  # Stretch!J79
    if roll_weight <= 0:
        return 0.0, 0.0, 0.0

    source = None
    if product["prestretch_source_product_id"]:
        source = db.execute(
            "SELECT * FROM product WHERE id=?", (product["prestretch_source_product_id"],)
        ).fetchone()

    # T79 = AI<source row> * J79 -- the source SKU's own finished, margin-
    # inclusive sales price per KG (i.e. unit_price_for(), not the raw
    # EX-Work cost), times this line's entered net (plastic) weight.
    source_sales_price = unit_price_for(db, source, country_class, customer_class,
                                         apply_extras=False) if source else 0.0
    material_cost = source_sales_price * net_weight  # T79

    # AC79 = I79 * (Core-prestretch rate EGP/kg / 'Material pricing'!F2)
    dollar_rate = cost_engine._get_setting(db, "dollar_rate_prestretch", 45)
    core_rate = cost_engine._material_rate(db, "core_prestretch")
    core_cost = core_weight * (core_rate / dollar_rate) if dollar_rate else 0.0  # AC79

    # AF79: conversion cost ("Depreciation + D.labor + Machine Power"),
    # reusing the same mechanism as every other product. Pre-Stretch's own
    # Stretch-Ability text ("Pre-Stretch") matches none of the power/regid
    # keywords cost_engine.roll_type_bucket() looks for, so it already
    # resolves to the Standard ("St") bucket -- kept as the documented,
    # deliberate choice (Pre-Stretch is a converting/rewinding step off the
    # Standard-bucket line, not its own extrusion process). Looked up by
    # this Pre-Stretch SKU's own micron (5/6/7/8/9/10/12); for microns with
    # no direct Electricity-sheet data point (5/6/7), the existing nearest-
    # micron fallback is used, same as any other under-specified micron
    # elsewhere in this app -- see COST_ENGINE.md.
    roll_type = cost_engine.roll_type_bucket(product["stretch_ability"])
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0
    conv_usd_per_ton = cost_engine.conversion_cost_usd_per_ton(db, micron, roll_type)
    other_costs = net_weight * conv_usd_per_ton / 1000  # AF79 (no width factor: Pre-Stretch has no Width field)

    return material_cost, core_cost, other_costs


def prestretch_packaging_cost_usd(db, rolls_per_pallet, net_weight, packaging_type):
    """Stretch!AD79. packaging_type: 'no_boxes' (D79=1) or 'boxes' (D79=2)."""
    if net_weight <= 0 or not rolls_per_pallet or rolls_per_pallet <= 0:
        return 0.0
    if packaging_type == "boxes":
        total = cost_engine._get_setting(db, "prestretch_packaging_boxes_usd", 14.38)
        dollar_rate = cost_engine._get_setting(db, "dollar_rate_prestretch", 45)
        box_rate = cost_engine._material_rate(db, "box")
        extra = (box_rate / dollar_rate / 6) if dollar_rate else 0.0
        return total / rolls_per_pallet + extra
    total = cost_engine._get_setting(db, "prestretch_packaging_noboxes_usd", 14.8)
    return total / rolls_per_pallet


def prestretch_ex_work_usd_kg(db, product, roll_weight_kg, core_weight_kg, rolls_per_pallet, packaging_type,
                               country_class, customer_class):
    """Stretch!AG79 = (AF79 + AD79 + AC79 + T79) / H79 -- the full Pre-Stretch
    EX-Work $/KG for one quotation line's entered inputs."""
    roll_weight = roll_weight_kg or 0
    if roll_weight <= 0:
        return 0.0
    net_weight = max(roll_weight - (core_weight_kg or 0), 0)
    material_cost, core_cost, other_costs = prestretch_cost_components(
        db, product, roll_weight_kg, core_weight_kg, country_class, customer_class
    )
    packaging_cost = prestretch_packaging_cost_usd(db, rolls_per_pallet, net_weight, packaging_type)
    total = material_cost + core_cost + other_costs + packaging_cost
    return round(total / roll_weight, 4)


def prestretch_unit_price_for(db, product, country_class, customer_class, roll_weight_kg, core_weight_kg,
                               rolls_per_pallet, packaging_type, price_adjustment_usd_kg=0, seller_type=None):
    roll_weight = roll_weight_kg or 0
    if roll_weight <= 0:
        return 0.0
    ex_work = prestretch_ex_work_usd_kg(
        db, product, roll_weight_kg, core_weight_kg, rolls_per_pallet, packaging_type,
        country_class, customer_class,
    )
    # Margin step (v18): Pre-Stretch has its own dedicated 'Prestretch'
    # film_type row in margin_factor (currently seeded at 0% for both
    # packaging variants), keyed by this line's packaging_type
    # ('boxes'/'no_boxes' -> 'Pre-stretch (Box)'/'Pre-stretch (No Box)').
    factor = cost_engine.margin_pct_for(db, product, prestretch_packaging_type=packaging_type)
    base = ex_work * (1 + factor)
    # Extras (v21): Pre-Stretch's own dedicated $/KG surcharge, plus the
    # same Color extra every other product gets (Pre-Stretch products can
    # still carry a non-Transparent catalog color).
    base += cost_engine.color_extra_usd_kg(db, product)
    base += cost_engine._get_setting(db, "extra_prestretch_usd_kg", 0.12)
    price = base + (price_adjustment_usd_kg or 0)
    price *= cost_engine.foreign_seller_extra_multiplier(db, seller_type)
    return round(price, 4)


def compute_prestretch_line(db, product, country_class, customer_class, quantity_pallets, roll_weight_kg,
                             core_weight_kg, rolls_per_pallet, packaging_type, price_adjustment_usd_kg=0,
                             pricing_basis="per_kg", seller_type=None):
    """Pre-Stretch counterpart of compute_line(): returns (unit_price_usd_kg, total_kg)
    from the rep's entered per-line roll weight / core weight / rolls-per-pallet /
    packaging type, instead of the product catalog's fixed values."""
    unit_price = prestretch_unit_price_for(
        db, product, country_class, customer_class, roll_weight_kg, core_weight_kg,
        rolls_per_pallet, packaging_type, price_adjustment_usd_kg, seller_type=seller_type,
    )
    gross_roll_weight = roll_weight_kg or 0
    net_roll_weight = max(gross_roll_weight - (core_weight_kg or 0), 0)
    roll_weight = net_roll_weight if pricing_basis == "net" else gross_roll_weight
    total_kg = round((quantity_pallets or 0) * (rolls_per_pallet or 0) * roll_weight, 3)
    return unit_price, total_kg
