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
                    round_result=True, credit_term=False, exclude_pallet_from_packaging=False,
                    convert_to_net_basis=False, box_packaging=True, margin_pct_override=None):
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
    end up including it, the same as Strap's FOB/CFR both do.

    margin_pct_override (v136): owner-requested "what price gives me what
    margin" tool (Admin/Pricing screen -- Arabic: "تسيبلي جمبه خانة فاضية
    اكتبلك فيها سعر تطلعلي ان السعر دا حيكون الفاكتور مثلاً 14% او 9%").
    When given (a plain fraction, e.g. 0.0 or 1.0 -- NOT a percent), this
    REPLACES the normal cost_engine.margin_pct_for() lookup + the discount-
    pct reduction (_discounted_factor()) entirely -- used only to probe two
    reference prices (at 0% and 100% margin) so app.py can hand the client
    two points on the price-vs-margin line for it to invert instantly for
    any price the rep types in, without a further round trip. None (the
    default) leaves normal pricing completely untouched."""
    if margin_pct_override is not None:
        factor = margin_pct_override
    else:
        factor = cost_engine.margin_pct_for(db, product, pallet_type=pallet_type,
                                             rolls_per_pallet_override=rolls_per_pallet_override, uv_type=uv_type)
        factor = _discounted_factor(factor, discount_pct)
    uv_fraction = cost_engine.UVI_FRACTION if uv_type else 0.0
    ex_work = cost_engine.compute_ex_work_usd_kg(db, product, pallet_type=pallet_type,
                                                  rolls_per_pallet_override=rolls_per_pallet_override,
                                                  uv_fraction=uv_fraction,
                                                  exclude_pallet_from_packaging=exclude_pallet_from_packaging,
                                                  box_packaging=box_packaging)
    base = ex_work * (1 + factor)
    if apply_extras:
        base += cost_engine.color_extra_usd_kg(db, colored)
    price = base + (price_adjustment_usd_kg or 0)
    if apply_extras:
        price *= cost_engine.foreign_seller_extra_multiplier(db, seller_type)
        price = cost_engine.apply_hidden_markup(price, hidden_markup_mode, hidden_markup_value)
        price += cost_engine.credit_term_extra_usd_kg(db, credit_term)
    # v117 -- convert_to_net_basis: Pre-Stretch's source-material row
    # (Stretch's "*J-pre" rows, e.g. row 40) finishes with a
    # *(H<row>/J<row>) multiplier -- the SAME gross-roll-weight/net-
    # (plastic-)weight ratio conversion compute_line()'s own 'net'
    # pricing_basis already applies elsewhere in this app (see
    # compute_line()'s docstring: "take the confirmed-correct Gross $/KG,
    # then re-divide the SAME total dollar amount by the NET... roll
    # weight"). Confirmed identical in effect: H<row>/J<row> is exactly
    # gross_roll_weight/net_roll_weight for that source row's OWN catalog
    # roll spec (never the consuming Pre-Stretch line's own roll spec).
    # Used only by pricing.prestretch_cost_components()'s source-price
    # lookup; every normal call leaves this False.
    if convert_to_net_basis:
        gross = product["roll_weight_kg"] or 0
        net = max(gross - (product["core_weight_kg"] or 0), 0)
        if net > 0:
            price = price * gross / net
    return cost_engine.round_half_up(price, 2) if round_result else price


def compute_line(db, product, country_class, customer_class, quantity_pallets, roll_size="standard",
                  price_adjustment_usd_kg=0, pallet_type=None, pricing_basis="per_kg",
                  roll_weight_kg=None, core_weight_kg=None, width_mm=None, rolls_per_pallet_override=None,
                  seller_type=None, auto_manual_override=None, colored=False, discount_pct=0, uv_type=None,
                  hidden_markup_mode=None, hidden_markup_value=0, round_result=True, credit_term=False,
                  box_packaging=True, margin_pct_override=None):
    """Returns (unit_price_usd_kg, total_kg).

    box_packaging (v120): this line's own "Box" checkbox -- True (default)
    prices Manual packing into a box (unchanged); False prices it as every-
    6-rolls-stretch-wrapped-without-a-box instead (owner-confirmed, reusing
    the sheet's own Pre-Stretch "No Boxes" packaging structure -- see
    cost_engine._pallet_key_for()'s matching v120 comment). No effect on
    Automatic lines (no box/no-box choice applies there).

    pricing_basis:
    v46 -- for every regular (non-Pre-Stretch) product, 'per_kg' and
    'gross' are the same total-weight basis -- the full (gross) roll
    weight, core included -- and that GROSS figure is the one confirmed
    to match the reference Excel sheet exactly (Stretch!AG/AI/AJ are all
    explicitly labelled "-gross weight" there; the sheet has no separate
    Net calculation anywhere for these regular rows).

    v100 -- owner-confirmed follow-up: 'net' is now ALSO offered for every
    regular product, not just Pre-Stretch -- computed with the exact same
    method as Pre-Stretch's own Net (compute_prestretch_line(), v47/v99):
    take the confirmed-correct Gross $/KG, then re-divide the SAME total
    dollar amount by the NET (core-excluded) roll weight instead of the
    gross one, so the $/KG shown rises accordingly while the $ total per
    roll is unchanged. The owner explicitly asked for this knowing the
    sheet has no Net figure to verify it against for regular products --
    unlike the Gross figure above (sheet-verified) and Pre-Stretch's Net
    (sheet-verified against Stretch!AO/AP), this Net figure for regular
    products is a first-party calculation with no sheet reference cell,
    so a "does this match the sheet" audit can only ever apply to it in
    the sense that this SAME formula pattern is what the sheet uses for
    Pre-Stretch -- there is no regular-product Net cell to diff against.

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
                                 credit_term=credit_term, box_packaging=box_packaging,
                                 margin_pct_override=margin_pct_override)
    # v46: the confirmed-correct, sheet-matching GROSS weight -- see this
    # function's docstring.
    gross_roll_weight = effective["roll_weight_kg"] or 0
    core_weight = effective["core_weight_kg"] or 0
    net_roll_weight = max(gross_roll_weight - core_weight, 0)

    # v100: owner-confirmed -- same re-divide-the-same-$-total-by-a-smaller-
    # weight method as Pre-Stretch's Net (see compute_prestretch_line()),
    # now applied to every regular product too when 'net' (a $/Roll view)
    # or 'net_per_kg' (a plain $/KG view -- v100) is selected; both mean
    # the same underlying Net $/KG, just displayed differently upstream.
    # v107 -- this used to round_half_up(..., 2) unconditionally, even when
    # round_result=False, which broke the "raw, unrounded" contract the
    # caller relies on for unit_price_usd_kg_raw.
    #
    # v110 -- v107 only half-fixed it. The owner reported the Net FOB $/KG
    # still didn't match her reference (render showed $1.74-1.75, reference
    # showed $1.76, for 23mic/150%Standard/16kg/46rpp) -- traced it to the
    # ORDER of operations, not just rounding: FOB must be built from the
    # GROSS raw price plus the FOB addon (that's what ROUNDUP()s to the
    # sheet-confirmed Gross FOB, Stretch!AO), and ONLY THEN, as the very
    # last step, re-divided over the net weight for a Net-basis display --
    # exactly the same order the app already uses for Pre-Stretch and for
    # this same function's own EX-Work Net figure (Gross computed+rounded
    # first, THEN net-converted). What v107 left in place instead net-
    # converted the RAW price BEFORE the FOB addon was ever added, so the
    # ROUNDUP() at the end was rounding a net-scaled number one full addon-
    # step below where it should have started, coming up a cent short.
    # Confirmed: rebuilding FOB as round(sheet-matching Gross FOB * gross_
    # roll_weight / net_roll_weight, 2) gives exactly $1.76 for the owner's
    # example. Fix: round_result=False (the "raw" call FOB is built from)
    # now returns the PURE GROSS raw price, completely untouched by
    # pricing_basis -- callers building FOB/CIF must add the addon and
    # ROUNDUP in gross terms first, then apply this SAME net_roll_weight/
    # gross_roll_weight ratio themselves as the final step (see app.py's
    # api_calculate_line and pricing.html's per-line FOB/CIF block, and
    # app.py's load_quotation()/build_pdf()/build_xlsx() for the saved-
    # quotation view, all updated to match). round_result=True (the plain
    # EX-Work $/KG shown in the table) keeps converting-then-rounding here,
    # since that's just Gross EX-Work (already rounded) re-divided by net
    # weight -- unaffected by this fix, confirmed unchanged in testing.
    if pricing_basis in ("net", "net_per_kg") and net_roll_weight > 0 and round_result:
        unit_price = cost_engine.round_half_up(unit_price * gross_roll_weight / net_roll_weight, 2)
        roll_weight = net_roll_weight
    else:
        roll_weight = gross_roll_weight

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
    # inclusive sales price per KG, times this line's entered net (plastic)
    # weight. AI<source row> is NOT the source SKU's plain/normal sales
    # price (Stretch row 39-style) -- the sheet prices it on a DEDICATED
    # "*J-pre" row (e.g. row 40) with two confirmed real differences from
    # the plain row (found by diffing every formula column of the plain vs
    # J-pre rows side by side against the reference H1.36 sheet):
    #   1) packaging cost excludes the source roll's own pallet-component
    #      line item (exclude_pallet_from_packaging=True -- see
    #      cost_engine.packaging_cost_per_roll_usd_excl_pallet()'s
    #      docstring for the business reason: this jumbo roll never ships
    #      on its own sales pallet, it feeds straight into the Pre-Stretch
    #      rewinding line).
    #   2) the result is converted from the source's own gross-roll-weight
    #      basis to its own net-(plastic-)weight basis before being reused
    #      here (convert_to_net_basis=True -- the sheet's own
    #      *(H<row>/J<row>) multiplier, using the SOURCE SKU's catalog roll
    #      weight/core weight, e.g. 50kg/1.8kg -- never this Pre-Stretch
    #      line's own entered roll/core weight, which is applied separately
    #      by the *net_weight below).
    # Verified this pattern (both differences) is identical across all 7 of
    # the sheet's Pre-Stretch source rows (40, 49, 27, 29, 31, 32, 35).
    # apply_extras=False (unchanged): a Pre-Stretch quote's own Color/
    # Foreign-seller/hidden-markup/credit-term extras must never be pulled
    # in a second time from the source SKU's own price lookup -- those are
    # applied once, on Pre-Stretch's own finished price, in
    # prestretch_unit_price_for(). round_result=False (unchanged in spirit
    # -- the sheet's AI<source row> is never rounded before feeding into
    # T<row>): keeps full precision through this internal lookup, exactly
    # matching the sheet's own unrounded formula chain.
    source_sales_price = unit_price_for(db, source, country_class, customer_class,
                                         apply_extras=False, exclude_pallet_from_packaging=True,
                                         convert_to_net_basis=True, round_result=False) if source else 0.0
    material_cost = source_sales_price * net_weight  # T79

    # AC79 = I79 * (Core-prestretch rate EGP/kg / 'Material pricing'!F2)
    dollar_rate = cost_engine._get_setting(db, "dollar_rate_prestretch", 45)
    core_rate = cost_engine._material_rate(db, "core_prestretch")
    core_cost = core_weight * (core_rate / dollar_rate) if dollar_rate else 0.0  # AC79

    # AF79: conversion cost ("Depreciation + D.labor + Machine Power").
    # v117 -- CORRECTED: this used to look up a real conversion-cost rate
    # here (same mechanism as every other product's own AF). Checked
    # directly against the H1.36 sheet's actual Stretch!AF column for
    # every one of the 7 Pre-Stretch rows (118-124), across every version
    # of the reference sheet the owner has sent (including today's
    # LibreOffice-recalculated test with real entered values): AF is
    # completely BLANK -- no formula at all, not even a zero-valued one --
    # for every Pre-Stretch row, in every version. So the sheet never
    # charges Pre-Stretch its own separate conversion cost at all -- makes
    # business sense given the rest of this function's own design: the
    # conversion cost of actually extruding/converting the plastic is
    # already baked into the SOURCE jumbo roll's own finished sales price
    # (material_cost/T79 above, via that source SKU's own AF), so charging
    # it a second time here would double-count it. The old non-zero
    # lookup was a plausible-looking but unverified assumption that turned
    # out to be a real, measurable overcharge (e.g. ~$0.31/roll extra on a
    # 12-micron/2kg-roll Pre-Stretch line) once actually checked against
    # the sheet with real numbers.
    other_costs = 0.0  # AF79 -- always 0, confirmed blank in the sheet for every Pre-Stretch row

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
    packaging type, instead of the product catalog's fixed values.

    v99: unit_price_usd_kg returned here is ALWAYS the Net-weight-based
    figure (matching the sheet's Stretch!AO/AP unconditionally -- see the
    comment below); pricing_basis only still matters for choosing how the
    $/Roll figure is displayed upstream (per_kg vs a whole-roll amount)."""
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

    # v99 -- owner-confirmed fix: Stretch!AO118/AP118 (Pre-Stretch's FOB/CFR
    # $/KG in the reference sheet) ALWAYS divide by the NET/plastic weight
    # (G118*J118) -- there is no "Gross" alternative anywhere in the sheet
    # for Pre-Stretch's FOB/CFR at all (confirmed via a full-sheet formula
    # check). This used to only apply when the rep explicitly picked "Net"
    # from the Price basis dropdown, defaulting to the lower Gross-based
    # figure otherwise -- a real, numerically-verified gap versus the sheet
    # (e.g. 1.89 $/KG gross-default vs 2.15 $/KG net for the same line) that
    # the owner confirmed should always match the sheet's Net figure. Now
    # applied unconditionally whenever net_roll_weight is available, exactly
    # like Excel -- the rep no longer needs to remember to pick "Net" for
    # the FOB/CFR $/KG shown to be correct; the "Price basis" dropdown only
    # picks $/Roll vs $/KG display from here on for Pre-Stretch (see the
    # matching "Gross" option removal for Pre-Stretch in pricing.html).
    # Keep the $ total per roll fixed (unit_price_gross * gross_weight) and
    # re-divide by the net weight, same math as before -- only the gate
    # controlling WHEN this applies has changed.
    if net_roll_weight > 0:
        unit_price = cost_engine.round_half_up(unit_price * gross_roll_weight / net_roll_weight, 2)
        roll_weight = net_roll_weight
    else:
        roll_weight = gross_roll_weight

    total_kg = cost_engine.round_half_up((quantity_pallets or 0) * (rolls_per_pallet or 0) * roll_weight, 3)
    return unit_price, total_kg
