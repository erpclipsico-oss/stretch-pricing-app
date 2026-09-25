"""PET Strap / PP Strap cost engine (v30, DB-backed costing as of v34).

Reverse-engineered directly from the owner's own PET_Export_pricing_1.25.xlsx
and PP_Export_pricing_1.32.xlsx cost sheets. Both sheets share the exact same
skeleton (BOM % -> material $/kg -> +electricity +fixed cost [+direct labor
for PP] -> Ex-Work; then Ex-Work*(1+profit)+core cost -> "coil price";
+Jwan/Stretch-wrap/Box/Cardboard/Pallet packaging add-ons -> Ex-Work Price
(Roll); +a per-roll share of a flat per-container FOB/shipping cost -> FOB
and CFR Roll prices; Credit terms add a flat $/kg surcharge), differing only
in their BOM composition, material rates, and (PP only) an extra Direct
Labor cost line and a resin-density-based meter-weight formula instead of
PET's flat one. That shared skeleton lives here once, parametrized per line
by LINE_CONFIG / BOM_DEFS below, rather than duplicating it twice.

Both sheets round every customer-facing price with Excel's ROUNDUP (always
rounds away from zero / up for positive numbers -- "never quote a fraction
of a cent under"), replicated here as `_roundup2`. Credit-term prices are
the one exception -- the sheets computed those with plain arithmetic, no
ROUNDUP, so this does the same.

Shared per-kg/per-piece EGP packaging materials (core, stretch wrap,
cardboard, pallet, box, jwan), the container FOB/shipping settings, the BOM
recipes (composition %, profit %, waste %) and the per-line electricity /
fixed cost / direct labor $-per-kg figures are all read live from the DB
(material_rate / global_setting / strap_bom / strap_line_config -- v34) so
every number in the cost chain is admin-editable. Only the *structure* of
each recipe -- which component keys exist and which material_rate suffix/
unit/currency each one maps to -- stays a Python constant here (COMPONENT_DEFS),
since that's a definition of what a BOM key means, not a price.
"""

import json
import math

CORE_WEIGHT_THRESHOLD_KG = 0.7

# v32 -- core outer-diameter -> core weight, owner-confirmed. Shared across
# both lines (a physical/geometric fact about the core tube itself, not a
# per-line cost input -- material_rate's "core" key is its $/kg price,
# this is its weight).
CORE_SIZES_MM = {
    "150": 0.25,
    "200": 0.5,
    "400-405": 1.0,
}

# v32 -- target gross-roll-weight window (net + core), owner-confirmed:
# "shouldn't exceed" this range. suggest_meters_per_coil() below solves for
# the meters/coil that lands right at the top of the window (maximises
# meters per coil without going over) given a computed g/m and core weight.
TARGET_GROSS_WEIGHT_KG = {
    "pet": (20.0, 20.2),
    "pp": (12.0, 12.2),
}


def suggest_meters_per_coil(line_key, gm_per_m, core_weight_kg):
    """Owner-confirmed rule: pick meters/coil so the gross roll weight
    (net + core) lands as close as possible to the top of the target
    window without exceeding it. Rounded down to the nearest 10m (coils
    are wound in practical round numbers, and rounding down -- never up --
    guarantees the max is never exceeded)."""
    if not gm_per_m or gm_per_m <= 0:
        return 0
    _, target_max = TARGET_GROSS_WEIGHT_KG.get(line_key, (0, 0))
    net_target_max_kg = target_max - (core_weight_kg or 0)
    if net_target_max_kg <= 0:
        return 0
    meters = net_target_max_kg * 1000.0 / gm_per_m
    return int(meters // 10) * 10

# v34 -- BOM composition %, profit % and waste % are now stored (and
# admin-editable) in the strap_bom DB table -- see _get_bom() below and
# db.STRAP_BOM_SEED for the seeded starting values (identical to what used
# to be hardcoded here). BOM_KEYS just lists which bom_keys are structurally
# valid for each line, for request validation, independent of their current
# DB-stored numbers.
BOM_KEYS = {
    "pet": ["pet_green", "pet_colors"],
    "pp": ["pure_white", "pure_color", "recycled_pure_white", "recycled_color", "recycled_pure_colors"],
}

# Human labels for each BOM key, in display order -- used by the "Custom
# (width x thickness)" strap-line builder so the rep picks Pure/Recycled x
# Colored/Not, rather than typing a bom_key.
BOM_LABELS = {
    "pet": [
        ("pet_green", "Pure (Green/Natural)"),
        ("pet_colors", "Colored"),
    ],
    "pp": [
        ("pure_white", "Pure - White"),
        ("pure_color", "Pure - Colored"),
        ("recycled_pure_white", "Recycled Pure - White"),
        ("recycled_color", "Recycled - Colored"),
        ("recycled_pure_colors", "Recycled Pure - Colored"),
    ],
}

# component key -> (material_rate key suffix, unit -- 'ton'|'kilo'|'piece',
#                    currency -- 'usd'|'egp', extra multiplier)
# 'ton' components are entered $/ton or EGP/ton and need /1000 to get to
# per-kg; 'kilo'/'piece' components (core/stretch/cardboard/pallet/box/jwan)
# are already priced per-kg or per-piece, used directly. A 'usd' component
# needs no /dollar_rate conversion; 'egp' does. The extra multiplier is the
# small scrap/waste surcharge each sheet bakes into its *main* resin only
# (1.04x for PET's "C4", 1.035x for PP's "5032 PP" -- mirrors the 1.04x the
# original Stretch Film engine already applies the same way). Structural
# (which component keys exist / what they map to), not a price -- stays a
# Python constant. electricity/fixed-cost/direct-labor per line and BOM
# composition/profit/waste are DB-stored (v34) -- see _get_line_config()
# and _get_bom() below.
LINE_CONFIG = {
    "pet": {
        "label": "PET Strap",
        "components": {
            "resin": ("resin", "ton", "egp", 1.0),
            "c4": ("c4", "ton", "usd", 1.04),
            "color": ("color", "ton", "egp", 1.0),
        },
    },
    "pp": {
        "label": "PP Strap",
        "components": {
            "5032": ("5032", "ton", "usd", 1.035),
            "coco3": ("coco3", "ton", "egp", 1.0),
            "recycled_colored": ("recycled_colored", "ton", "egp", 1.0),
            "recycled_pure": ("recycled_pure", "ton", "egp", 1.0),
            "color": ("color", "ton", "egp", 1.0),
        },
    },
}

# Packaging factor (margin on top of each packaging material's raw $ cost) --
# identical across both lines/sheets.
PACKAGING_FACTORS = {"core": 0.2, "stretch": 0.1, "cardboard": 0.1, "pallet": 0.1, "box": 0.1, "jwan": 0.1}


def _roundup2(x):
    if x is None:
        return 0.0
    return math.ceil(x * 100 - 1e-9) / 100


def _get_setting(conn, key, default=0.0):
    row = conn.execute("SELECT value FROM global_setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None and row["value"] is not None else default


def _material_rate(conn, line_key, suffix):
    row = conn.execute(
        "SELECT value FROM material_rate WHERE material_key=?", (f"{line_key}_{suffix}",)
    ).fetchone()
    return row["value"] if row else 0.0


def _dollar_rate(conn):
    # v88 -- owner-requested split: independent from Stretch Film's own
    # "dollar_rate" (Global Cost Settings) -- see db.py's strap_dollar_rate
    # seed note. Editable on the PET/PP Strap Costing page.
    return _get_setting(conn, "strap_dollar_rate", 45)


def _get_bom(conn, line_key, bom_key):
    """DB-backed BOM recipe (v34) -- components/profit/waste, admin-editable
    via the Strap Costing page. Falls back to an all-zero recipe if the row
    is somehow missing (should not happen once seeded) rather than raising,
    so a calculate just prices to $0 instead of crashing."""
    row = conn.execute(
        "SELECT profit_pct, waste_pct, components_json FROM strap_bom WHERE line_key=? AND bom_key=?",
        (line_key, bom_key),
    ).fetchone()
    if not row:
        return {"components": {}, "profit": 0.0, "waste": 0.0}
    return {
        "components": json.loads(row["components_json"]),
        "profit": row["profit_pct"],
        "waste": row["waste_pct"],
    }


def _get_line_config(conn, line_key):
    """DB-backed electricity/fixed-cost/direct-labor figures (v34),
    admin-editable via the Strap Costing page.

    v87 -- fixed_cost_per_ton_egp/direct_labor_per_ton_egp (EGP, like
    electricity_per_ton_egp) are the live inputs now; compute_strap_line()
    divides them by the current Dollar Rate at calc time, matching both
    reference workbooks' own live Fixed Cost (kg) USD / Direct Labor (kg)
    USD formulas exactly (see db.py's strap_line_config migration note for
    why this replaced the old frozen fixed_cost_per_kg_usd/
    direct_labor_per_kg_usd constants)."""
    row = conn.execute(
        """SELECT electricity_per_ton_egp, fixed_cost_per_ton_egp, direct_labor_per_ton_egp
           FROM strap_line_config WHERE line_key=?""",
        (line_key,),
    ).fetchone()
    if not row:
        return {"electricity_per_ton_egp": 0.0, "fixed_cost_per_ton_egp": 0.0, "direct_labor_per_ton_egp": 0.0}
    return dict(row)


def meter_weight_g_per_m(line_key, product, bom_components):
    """Physical weight-per-meter of the strap, g/m -- PET uses a flat
    width/thickness formula; PP additionally weighs the mix's density from
    its own resin composition (denser components -> heavier per m³)."""
    width, thick = product["width_mm"], product["thickness_mm"]
    if not (width and thick):
        return 0.0
    if line_key == "pet":
        if width <= 12:
            return width * (thick - 0.1) * 1.385
        return (width - 0.5) * (thick - 0.12) * 1.385
    # PP: density-weighted by resin mix (coco3 is denser: x1.5; everything
    # else in the base-resin family: x0.9), net width -0.2mm, net thickness
    # x0.7 (matches the sheet's Net Width / Net Thickness columns exactly).
    coco3_frac = bom_components.get("coco3", 0.0)
    other_frac = (bom_components.get("5032", 0.0) + bom_components.get("recycled_colored", 0.0)
                  + bom_components.get("recycled_pure", 0.0))
    density = coco3_frac * 1.5 + other_frac * 0.9
    net_width = width - 0.2
    net_thickness = thick * 0.7
    return net_width * net_thickness * density


def _packaging_addons(conn, line_key, dollar_rate, core_weight_kg, has_box, has_pallet):
    """Jwan / Stretch-wrap / Box / Cardboard / Pallet per-roll cost, exactly
    matching each sheet's own X:AB column formulas (verified against the
    PET sheet's stored values before shipping -- see COST_ENGINE notes)."""
    def rate(suffix):
        return _material_rate(conn, line_key, suffix) / dollar_rate if dollar_rate else 0.0

    small = core_weight_kg < CORE_WEIGHT_THRESHOLD_KG
    f = PACKAGING_FACTORS
    if core_weight_kg <= 0:
        return 0.0

    jwan = rate("jwan") * 2 * (1 + f["jwan"])

    stretch_rate = rate("stretch")
    if small and not has_box:
        stretch = stretch_rate * 0.005 + stretch_rate * 1.2 / 66
    elif small and has_box:
        stretch = stretch_rate * 0.005 + stretch_rate * 1.2 / 33
    elif not small and not has_box:
        stretch = stretch_rate * 0.01 + stretch_rate * 1.2 / 52
    else:  # not small and has_box
        stretch = stretch_rate * 0.01 + stretch_rate * 1.2 / 52
    stretch *= (1 + f["stretch"])

    box_rate = rate("box")
    if has_box and small:
        box = (box_rate * (1 + f["box"])) / 2
    elif has_box and not small:
        box = box_rate * (1 + f["box"])
    else:
        box = 0.0

    cardboard_rate = rate("cardboard")
    if not has_box and has_pallet and small:
        cardboard = (cardboard_rate * 14 * (1 + f["cardboard"])) / 66
    elif not has_box and has_pallet and not small:
        cardboard = (cardboard_rate * 14 * (1 + f["cardboard"])) / 52
    else:
        cardboard = 0.0

    pallet_rate = rate("pallet")
    if has_pallet and small and not has_box:
        pallet = (pallet_rate * (1 + f["pallet"])) / 66
    elif has_pallet and small and has_box:
        pallet = (pallet_rate * (1 + f["pallet"])) / 72
    elif has_pallet and not small and not has_box:
        pallet = (pallet_rate * (1 + f["pallet"])) / 52
    elif has_pallet and not small and has_box:
        pallet = (pallet_rate * (1 + f["pallet"])) / 52
    else:
        pallet = 0.0

    return jwan + stretch + box + cardboard + pallet


def suggest_rolls_per_pallet(core_weight_kg, has_box):
    """The rolls/pallet figure implicit in each sheet's own container-share
    formula (the "72"/"66"/"52" divisors), surfaced as its own value so a
    rep can see -- and override -- it directly instead of it staying
    buried inside the FOB/CFR math. Purely a quantity-conversion default
    (Qty (pallets) x Rolls/pallet -> total coils); overriding it does not
    change the container-freight math itself, which keeps using the
    verified per-sheet formula in _container_share below."""
    small = core_weight_kg < CORE_WEIGHT_THRESHOLD_KG
    if small:
        return 72 if has_box else 66
    return 52


def suggest_pallets_per_container(core_weight_kg, has_box, ctr20, ctr40):
    """The Pallets/Container figure shown to a rep/customer (quote builder
    live preview + the printed PDF/Excel/view page) -- purely informational,
    read-only.

    v91 -- owner-confirmed, twice, explicitly overriding the small-core
    (<0.7kg) 11/22/24 split this used to return (which came from the
    per-sheet container-SHARE formula in _container_share below, used to
    spread the flat per-container FOB/freight cost across each coil -- see
    that function's own docstring). The owner was clear that figure is not
    her real max loading and she never asked for that split: her own
    stated max loading for PET/PP Strap is a flat 20 pallets/40ft container,
    10 pallets/20ft container, full stop -- no exception for a lighter
    core weight or for Box vs No-Box. This function now just returns that.

    Deliberately NOT touched: _container_share()/compute_strap_line()'s
    actual FOB/CFR $ math, which keeps dividing by the verified per-sheet
    11/22/24/10/20 split (still exactly matches PET_Export_pricing /
    PP_Export_pricing's own formulas) -- the owner's correction was about
    what number gets PRINTED as Pallets/Container, not about the $ price,
    which she has not disputed."""
    if ctr20:
        return 10
    if ctr40:
        return 20
    return 0


def _container_share(rate_usd, core_weight_kg, ctr20, ctr40, has_box):
    if core_weight_kg <= 0 or not (ctr20 or ctr40):
        return 0.0
    small = core_weight_kg < CORE_WEIGHT_THRESHOLD_KG
    if small and ctr20 and has_box:
        return rate_usd / (11 * 72)
    if small and ctr20:
        return rate_usd / (11 * 66)
    if small and ctr40 and has_box:
        return rate_usd / (22 * 72)
    if small and ctr40:
        return rate_usd / (24 * 66)
    if not small and ctr20:
        return rate_usd / (10 * 52)
    if not small and ctr40:
        return rate_usd / (20 * 52)
    return 0.0


def compute_strap_line(conn, line_key, product, discount_pct=0, credit_term=False,
                        hidden_markup_mode=None, hidden_markup_value=0,
                        fob_container_usd=None, shipping_container_usd=None):
    """Full per-roll/per-kg breakdown for one strap_product row. Returns a
    dict with ex_work_price_roll, fob_price_roll/kg, cfr_price_roll/kg
    (Cash terms unless credit_term=True, in which case the flat $/kg
    surcharge is already included), plus gross_weight_kg (for converting
    a quantity of coils into total_kg elsewhere).

    fob_container_usd/shipping_container_usd: the per-container FOB
    handling / international-freight $ amount to spread across this line
    (via _container_share() below).

    shipping_container_usd (v61): the CALLER is expected to supply this
    looked up from the SAME admin-editable Freight table Stretch Film
    uses (see app.py's _freight_for_destination()), keyed by the
    quotation's own Destination selection, instead of each strap line
    silently pricing every shipment through one hardcoded destination.

    fob_container_usd (v62 -- reverted from v61's brief shared-Loading-
    Ports experiment per the owner's clarification: only the shipping/
    freight leg is shared with Stretch Film, not FOB): left as its own
    separate flat per-container setting -- every in-app call site now
    always passes None here, which falls back to the admin-editable
    strap_fob_cost_per_container_usd setting (Admin > PET/PP Strap
    Costing), same as before v61.

    hidden_markup_mode/hidden_markup_value (v44): a per-user hidden markup,
    from user.markup_mode/markup_value (generalizes the old Strap-only
    strap_markup_pct to also cover Stretch Film -- see
    cost_engine.apply_hidden_markup() and pricing.py's unit_price_for()).
    'percent' mode is applied at the exact same point discount_pct is (the
    inverse of a discount), so it flows through into FOB/CFR/total the
    same consistent way the old strap_markup_pct did. 'cents_per_kg' mode
    is a flat USD/KG amount added onto the FINAL fob/cfr $/KG price,
    mirroring how the credit-term surcharge below is applied. Neither is
    surfaced anywhere in the price breakdown -- see app.py's
    _calculate_strap_line/api_save_quotation."""
    cfg = LINE_CONFIG[line_key]
    line_numbers = _get_line_config(conn, line_key)
    bom = _get_bom(conn, line_key, product["bom_key"])
    components = bom["components"]
    dollar_rate = _dollar_rate(conn)

    net_weight_g_per_m = meter_weight_g_per_m(line_key, product, components)
    roll_net_kg = product["meters_per_coil"] * net_weight_g_per_m / 1000.0
    core_weight_kg = product["core_weight_kg"] or 0
    gross_weight_kg = roll_net_kg + core_weight_kg

    material_cost = 0.0
    for comp_key, frac in components.items():
        suffix, unit, currency, multiplier = cfg["components"][comp_key]
        rate = _material_rate(conn, line_key, suffix)
        if currency == "egp":
            rate = rate / dollar_rate if dollar_rate else 0.0
        cost = roll_net_kg * frac * rate / 1000.0 * multiplier
        material_cost += cost
    material_cost = _roundup2(material_cost * (1 + bom["waste"]))

    electricity_cost = (roll_net_kg * line_numbers["electricity_per_ton_egp"] / 1000.0 / dollar_rate
                         if dollar_rate else 0.0)
    # v87 -- direct_labor/fixed_cost now divide by the LIVE dollar_rate too,
    # same as electricity above (see _get_line_config()'s docstring).
    direct_labor_cost = (roll_net_kg * line_numbers["direct_labor_per_ton_egp"] / 1000.0 / dollar_rate
                          if dollar_rate else 0.0)
    fixed_cost = (roll_net_kg * line_numbers["fixed_cost_per_ton_egp"] / 1000.0 / dollar_rate
                  if dollar_rate else 0.0)
    ex_work_roll = material_cost + electricity_cost + direct_labor_cost + fixed_cost

    # v93 -- owner-confirmed: a Discount % must never be able to eat into
    # actual production cost, in Strap exactly as in Stretch Film -- it may
    # only ever come off the profit margin, floored at 0 so the price can
    # never drop below ex_work_roll + core + packaging (raw cost) no matter
    # how large the discount is. This used to be a flat (1 - discount%)
    # multiplied across the WHOLE (packaging + coil_price) total, which had
    # no such floor: with a low enough profit_pct on a given BOM recipe (or
    # a high enough discount), that could reach past the margin and into
    # real cost. Now the discount comes off the BOM's own profit_pct first
    # (same mechanism as pricing._discounted_factor() for Stretch Film),
    # and only that discounted profit feeds coil_price -- packaging and
    # core cost are never discounted at all, matching Electricity/Fixed
    # Cost/Direct Labor never being discounted either. At discount_pct=0
    # this is numerically identical to the old formula, so today's prices
    # are unchanged.
    discounted_profit_pct = max((bom["profit"] or 0) - (discount_pct or 0) / 100.0, 0.0)
    core_rate = _material_rate(conn, line_key, "core") / dollar_rate if dollar_rate else 0.0
    coil_price = ex_work_roll * (1 + discounted_profit_pct) + core_weight_kg * core_rate * (1 + PACKAGING_FACTORS["core"])

    packaging_total = _packaging_addons(
        conn, line_key, dollar_rate, core_weight_kg, bool(product["has_box"]), bool(product["has_pallet"])
    )

    markup_pct = (hidden_markup_value or 0) if hidden_markup_mode == "percent" else 0
    if core_weight_kg > 0:
        ex_work_price_roll = _roundup2(
            (packaging_total + coil_price) * (1 + (markup_pct or 0) / 100.0)
        )
    else:
        ex_work_price_roll = 0.0

    if fob_container_usd is None:
        fob_container_usd = _get_setting(conn, "strap_fob_cost_per_container_usd", 1100)
    if shipping_container_usd is None:
        shipping_container_usd = _get_setting(conn, "strap_shipping_rate_per_container_usd", 1200)
    ctr20, ctr40 = bool(product["ctr20"]), bool(product["ctr40"])
    has_box = bool(product["has_box"])

    if core_weight_kg > 0 and (ctr20 or ctr40):
        fob_price_roll = _roundup2(
            ex_work_price_roll + _container_share(fob_container_usd, core_weight_kg, ctr20, ctr40, has_box)
        )
        cfr_price_roll = _roundup2(
            fob_price_roll + _container_share(shipping_container_usd, core_weight_kg, ctr20, ctr40, has_box)
        )
    else:
        fob_price_roll = 0.0
        cfr_price_roll = 0.0

    ex_work_price_kg = ex_work_price_roll / gross_weight_kg if gross_weight_kg else 0.0
    fob_price_kg = fob_price_roll / gross_weight_kg if gross_weight_kg else 0.0
    cfr_price_kg = cfr_price_roll / gross_weight_kg if gross_weight_kg else 0.0

    # v44 -- 'cents_per_kg' mode: a flat hidden USD/KG amount, added
    # straight onto the finished fob/cfr $/KG price (same mechanism as the
    # credit-term surcharge just below), then the roll figures are
    # re-derived from the adjusted $/KG so everything stays consistent.
    if hidden_markup_mode == "cents_per_kg" and (hidden_markup_value or 0):
        markup_amt = hidden_markup_value
        if fob_price_roll > 0:
            fob_price_kg = fob_price_kg + markup_amt
            fob_price_roll = fob_price_kg * gross_weight_kg
        if cfr_price_roll > 0:
            cfr_price_kg = cfr_price_kg + markup_amt
            cfr_price_roll = cfr_price_kg * gross_weight_kg

    if credit_term:
        surcharge = _get_setting(conn, "strap_credit_surcharge_usd_kg", 0.03)
        if fob_price_roll > 0:
            fob_price_kg = fob_price_kg + surcharge
            fob_price_roll = fob_price_kg * gross_weight_kg
        if cfr_price_roll > 0:
            cfr_price_kg = cfr_price_kg + surcharge
            cfr_price_roll = cfr_price_kg * gross_weight_kg

    return {
        "gross_weight_kg": gross_weight_kg,
        "net_weight_kg": roll_net_kg,
        "meter_weight_g_per_m": net_weight_g_per_m,
        "material_cost": material_cost,
        "electricity_cost": electricity_cost,
        "direct_labor_cost": direct_labor_cost,
        "fixed_cost": fixed_cost,
        "ex_work_roll": ex_work_roll,
        "coil_price": coil_price,
        "packaging_total": packaging_total,
        "ex_work_price_roll": ex_work_price_roll,
        "fob_price_roll": fob_price_roll,
        "cfr_price_roll": cfr_price_roll,
        "ex_work_price_kg": ex_work_price_kg,
        "fob_price_kg": fob_price_kg,
        "cfr_price_kg": cfr_price_kg,
    }
