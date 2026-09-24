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
    # v41 -- uses the MICRO SIGN (U+00B5, "µ") rather than the Greek small
    # letter mu (U+03BC, "μ") that used to be here: they look identical in
    # a browser, but reportlab's PDF export uses the built-in Helvetica
    # font (WinAnsi encoding), which has no glyph for U+03BC -- it silently
    # fell back to a bare "m", corrupting every micron label in the PDF
    # (e.g. "17μm" -> "17mm"). U+00B5 IS in WinAnsi and renders correctly
    # in both the web UI and the PDF/Excel exports.
    return f"{product['micron']}µm – {product['stretch_ability']}"


def disambiguate_labels(products):
    """v28 -- several products share an identical micron+stretch_ability
    combo but differ only in roll_weight_kg (e.g. a 16kg standard sales
    roll vs a 50kg jumbo roll used as the source for prestretch products).
    product_label() alone can't tell them apart, so the /pricing product
    picker was showing the exact same text twice for two different
    products with different prices -- a user could pick the wrong one by
    accident with no way to notice. This appends the roll weight only to
    the labels that actually collide, leaving every non-colliding label
    unchanged. Returns a list of label strings, same order/length as
    `products`."""
    from collections import Counter

    base_labels = [product_label(p) for p in products]
    counts = Counter(base_labels)
    out = []
    for p, base in zip(products, base_labels):
        if counts[base] > 1:
            rw = p["roll_weight_kg"]
            rw_str = (f"{rw:g}" if isinstance(rw, (int, float)) else str(rw)) if rw is not None else "?"
            out.append(f"{base} ({rw_str}kg roll)")
        else:
            out.append(base)
    return out


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


def _discounted_factor(factor, discount_pct):
    """v27: a quotation's Discount % (per-line + global, added together in
    percentage points) comes off the margin factor itself, never off the
    EX-Work cost -- e.g. a 20% margin with a 5% discount becomes a 15%
    margin, so price = EX-Work * 1.15 instead of EX-Work * 1.20. Floored at
    0% so a discount alone can never push the price below EX-Work cost +
    extras + adjustment (the discount eats into profit only, confirmed with
    the business owner -- previously discount was a flat % off the whole
    finished price, which ate into cost too)."""
    return max((factor or 0) - (discount_pct or 0) / 100.0, 0.0)


def unit_price_for(db, product, country_class, customer_class, roll_size="standard", price_adjustment_usd_kg=0,
                    pallet_type=None, rolls_per_pallet_override=None, seller_type=None, apply_extras=True,
                    colored=False, discount_pct=0, uv_type=None, hidden_markup_mode=None, hidden_markup_value=0,
                    round_result=True, credit_term=False):
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
    colored: this quotation LINE's own "Colored" checkbox (v21.1) -- drives
    the Extras "Color extra" $/KG surcharge; not a property of the product.
    discount_pct: this line's own Discount % plus the quotation's Global
    Discount %, added together -- see _discounted_factor().
    `product["auto_manual"]` is expected to already reflect the line's own
    Packing type choice (Automatic vs a Manual variant), not necessarily
    the catalog default -- see compute_line()/cost_engine.with_overrides().
    apply_extras: set False for an internal lookup of another product's own
    price (e.g. Pre-Stretch borrowing its source SKU's sales price) so the
    Extras surcharges aren't silently double-applied; every top-level quote
    line leaves this at the default True.
    uv_type (v36): this quotation LINE's own UV variant selection (a key
    from cost_engine.UV_TYPES, or None) -- overrides the margin film_type
    lookup and adds the flat UVI_FRACTION material cost; see
    cost_engine.margin_pct_for()/compute_ex_work_usd_kg().
    hidden_markup_mode/hidden_markup_value (v44): the priced-for user's own
    HIDDEN markup (user.markup_mode/markup_value) -- applied last, on top
    of everything else including the Foreign Seller multiplier, so it's
    never surfaced anywhere in the UI/PDF/Excel breakdown. Gated behind
    apply_extras same as the Foreign Seller multiplier, so it isn't
    silently double-applied when this is an internal lookup of another
    product's own price (e.g. Pre-Stretch borrowing its source SKU's sales
    price) -- see cost_engine.apply_hidden_markup().

    round_result (v70.2): False returns the raw, unrounded float instead of
    the usual round_half_up(price, 2). Needed so the FOB/CIF $/KG add-on
    step (cost_engine.round_up() in app.py) can add the port/freight
    add-on to the SAME full-precision EX-Work price the H1.36 workbook's
    own Stretch!AI column keeps (never rounded there either -- see
    cost_engine.round_up()'s docstring) before rounding up once at the
    end, instead of rounding EX-Work to 2dp first and only then adding the
    add-on, which is the extra rounding step that was making this app's
    FOB $/KG land a cent below the workbook's for almost every SKU.

    credit_term (v79): the quotation's own Payment Term flag (not Cash) --
    adds the admin-configured 'Extras > Credit payment terms extra' $/KG
    surcharge, the Stretch Film counterpart of the credit-term surcharge
    PET/PP Strap has always had (strap_pricing.compute_strap_line()).
    Gated behind apply_extras like every other Extras surcharge, so it
    isn't double-applied on an internal lookup (e.g. Pre-Stretch borrowing
    its source SKU's own price) -- and because it flows into the raw,
    unrounded price returned when round_result=False, it's already baked
    into the EX-Work base that FOB/CIF get built from in app.py, so both
    end up including it, the same as Strap's FOB/CFR both do."""
    factor = cost_engine.margin_pct_for(db, product, pallet_type=pallet_type,
                                         rolls_per_pallet_override=rolls_per_pallet_override, uv_type=uv_type)
    factor = _discounted_factor(factor, discount_pct)
    uv_fraction = cost_engine.UVI_FRACTION if uv_type else 0.0
    ex_work = cost_engine.compute_ex_work_usd_kg(db, product, pallet_type=pallet_type,
                                                  rolls_per_pallet_override=rolls_per_pallet_override,
                                                  uv_fraction=uv_fraction)
    base = ex_work * (1 + factor)
    if apply_extras:
        base += cost_engine.color_extra_usd_kg(db, colored)
    price = base + (price_adjustment_usd_kg or 0)
    if apply_extras:
        price *= cost_engine.foreign_seller_extra_multiplier(db, seller_type)
        price = cost_engine.apply_hidden_markup(price, hidden_markup_mode, hidden_markup_value)
        price += cost_engine.credit_term_extra_usd_kg(db, credit_term)
    return cost_engine.round_half_up(price, 2) if round_result else price


def compute_line(db, product, country_class, customer_class, quantity_pallets, roll_size="standard",
                  price_adjustment_usd_kg=0, pallet_type=None, pricing_basis="per_kg",
                  roll_weight_kg=None, core_weight_kg=None, width_mm=None, rolls_per_pallet_override=None,
                  seller_type=None, auto_manual_override=None, colored=False, discount_pct=0, uv_type=None,
                  hidden_markup_mode=None, hidden_markup_value=0, round_result=True, credit_term=False):
    """Returns (unit_price_usd_kg, total_kg).

    pricing_basis (v46 -- owner-confirmed): for every regular (non-Pre-
    Stretch) product, 'per_kg' / 'gross' / 'net' are ALL the same total-
    weight basis -- the full (gross) roll weight, core included. The Net
    basis genuinely differs (gross minus core) ONLY for Pre-Stretch (see
    compute_prestretch_line() below) -- every roll always ships with its
    core, so for every other product line the owner does not want the
    core weight silently dropped out of what the customer is billed for
    just because "Net" was picked in the dropdown. The dropdown still
    offers Gross/Net/KG (and existing saved quotations keep whichever
    basis they were saved with) purely as a display label ($/Roll vs
    $/KG) -- it no longer changes the total KG/price for these products.

    roll_weight_kg / core_weight_kg / width_mm / rolls_per_pallet_override:
    per-quotation-line overrides of the product catalog's defaults, since
    the actual roll weight, core weight, width and (for non-standard
    weights) rolls/pallet vary by customer order for EVERY product, not
    just Pre-Stretch (see cost_engine.with_overrides()). None means "use
    the catalog value" for each.
    """
    effective = cost_engine.with_overrides(product, roll_weight_kg, core_weight_kg, width_mm,
                                            auto_manual=auto_manual_override)
    rolls_per_pallet = cost_engine.effective_rolls_per_pallet(db, effective, pallet_type, rolls_per_pallet_override)
    unit_price = unit_price_for(db, effective, country_class, customer_class, roll_size, price_adjustment_usd_kg,
                                 pallet_type=pallet_type, rolls_per_pallet_override=rolls_per_pallet_override,
                                 seller_type=seller_type, colored=colored, discount_pct=discount_pct,
                                 uv_type=uv_type, hidden_markup_mode=hidden_markup_mode,
                                 hidden_markup_value=hidden_markup_value, round_result=round_result,
                                 credit_term=credit_term)
    # v46: always the full gross roll weight for every regular product --
    # see this function's docstring. (Pre-Stretch is the one place the
    # Net basis actually subtracts the core -- compute_prestretch_line().)
    roll_weight = effective["roll_weight_kg"] or 0
    total_kg = cost_engine.round_half_up((quantity_pallets or 0) * rolls_per_pallet * roll_weight, 3)
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
    return cost_engine.round_half_up(total / roll_weight, 4)


def prestretch_unit_price_for(db, product, country_class, customer_class, roll_weight_kg, core_weight_kg,
                               rolls_per_pallet, packaging_type, price_adjustment_usd_kg=0, seller_type=None,
                               colored=False, discount_pct=0, hidden_markup_mode=None, hidden_markup_value=0,
                               credit_term=False):
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
    # v27: discount comes off this margin factor too, same rule as every
    # other product -- see _discounted_factor().
    factor = _discounted_factor(factor, discount_pct)
    base = ex_work * (1 + factor)
    # Extras (v21): Pre-Stretch's own dedicated $/KG surcharge, plus the
    # same Color extra every other product gets (v21.1: driven by the
    # line's own "Colored" checkbox, not the catalog color).
    base += cost_engine.color_extra_usd_kg(db, colored)
    base += cost_engine._get_setting(db, "extra_prestretch_usd_kg", 0.12)
    price = base + (price_adjustment_usd_kg or 0)
    price *= cost_engine.foreign_seller_extra_multiplier(db, seller_type)
    # v44: hidden per-user markup, applied last -- see unit_price_for()'s
    # matching comment.
    price = cost_engine.apply_hidden_markup(price, hidden_markup_mode, hidden_markup_value)
    # v79: same 'Extras > Credit payment terms extra' surcharge Stretch
    # Film's unit_price_for() gets, applied last like it is there.
    price += cost_engine.credit_term_extra_usd_kg(db, credit_term)
    return cost_engine.round_half_up(price, 2)


def compute_prestretch_line(db, product, country_class, customer_class, quantity_pallets, roll_weight_kg,
                             core_weight_kg, rolls_per_pallet, packaging_type, price_adjustment_usd_kg=0,
                             pricing_basis="per_kg", seller_type=None, colored=False, discount_pct=0,
                             hidden_markup_mode=None, hidden_markup_value=0, credit_term=False):
    """Pre-Stretch counterpart of compute_line(): returns (unit_price_usd_kg, total_kg)
    from the rep's entered per-line roll weight / core weight / rolls-per-pallet /
    packaging type, instead of the product catalog's fixed values."""
    # unit_price is always computed on GROSS weight -- Stretch!AI79 is
    # explicitly labelled "-gross weight" in the source sheet, and every
    # EX-Work-stage figure (AG79, AI79, AJ79=AI79*H79) is built off the
    # gross roll weight H79. There is no separate "Net" EX-Work price in
    # the sheet.
    unit_price = prestretch_unit_price_for(
        db, product, country_class, customer_class, roll_weight_kg, core_weight_kg,
        rolls_per_pallet, packaging_type, price_adjustment_usd_kg, seller_type=seller_type, colored=colored,
        discount_pct=discount_pct, hidden_markup_mode=hidden_markup_mode, hidden_markup_value=hidden_markup_value,
        credit_term=credit_term,
    )
    gross_roll_weight = roll_weight_kg or 0
    net_roll_weight = max(gross_roll_weight - (core_weight_kg or 0), 0)

    if pricing_basis == "net" and net_roll_weight > 0:
        # v47: Pre-Stretch is the one place a genuinely different Net $/KG
        # is computed -- confirmed against Stretch!AO79/AP79 (FOB/CFR $/KG),
        # which divide the SAME container-level $ total (itself anchored to
        # the gross-weight EX-Work price, AJ79=AI79*H79) by the container's
        # NET/plastic weight (G79*J79) instead of its gross weight. Same $
        # spread over less weight -> a higher $/KG. We reproduce that at the
        # roll level: keep the $ total per roll fixed (unit_price_gross *
        # gross_weight) and re-divide by the net weight, so switching a
        # Pre-Stretch line to the Net basis raises the $/KG shown (this is
        # the "price used to be high on Net" behaviour the owner recalled)
        # while the total quoted amount for that line is unchanged.
        unit_price = cost_engine.round_half_up(unit_price * gross_roll_weight / net_roll_weight, 2)
        roll_weight = net_roll_weight
    else:
        roll_weight = gross_roll_weight

    total_kg = cost_engine.round_half_up((quantity_pallets or 0) * (rolls_per_pallet or 0) * roll_weight, 3)
    return unit_price, total_kg
