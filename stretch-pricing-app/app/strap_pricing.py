"""PET Strap / PP Strap cost engine (v30).

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
cardboard, pallet, box, jwan) and the container FOB/shipping settings are
read live from the DB (material_rate / global_setting) so they stay
admin-editable exactly like every other cost input in this app. The BOM
composition percentages and per-line fixed cost / electricity / direct
labor $-per-kg constants are the one part frozen here as Python constants
(matching how Stretch Film's own BOM tables and Fixed-Cost-per-kg figure
work) -- see LINE_CONFIG.
"""

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

# Composition fractions of each raw-material *rate key* (matched against
# material_rate.material_key with the line's own "pet_"/"pp_" prefix),
# the profit margin applied to Ex-Work before container freight, and the
# waste% applied on top of the raw material cost. profit/waste values and
# component keys come directly from each sheet's own "Material cost" tab.
BOM_DEFS = {
    "pet": {
        "pet_green": {"components": {"resin": 0.96, "c4": 0.02, "color": 0.02},
                      "profit": 0.16, "waste": 0.01},
        "pet_colors": {"components": {"resin": 0.935, "c4": 0.02, "color": 0.045},
                       "profit": 0.16, "waste": 0.01},
    },
    "pp": {
        "pure_white": {"components": {"5032": 0.97, "coco3": 0.03},
                       "profit": 0.12, "waste": 0.04},
        "pure_color": {"components": {"5032": 0.955, "color": 0.045},
                       "profit": 0.12, "waste": 0.08},
        "recycled_pure_white": {"components": {"5032": 0.5, "recycled_pure": 0.45, "coco3": 0.05},
                                 "profit": 0.16, "waste": 0.04},
        "recycled_color": {"components": {"recycled_colored": 0.99, "color": 0.01},
                            "profit": 0.20, "waste": 0.08},
        "recycled_pure_colors": {"components": {"5032": 0.5, "recycled_pure": 0.45, "color": 0.05},
                                  "profit": 0.16, "waste": 0.08},
    },
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
# original Stretch Film engine already applies the same way).
LINE_CONFIG = {
    "pet": {
        "label": "PET Strap",
        "components": {
            "resin": ("resin", "ton", "egp", 1.0),
            "c4": ("c4", "ton", "usd", 1.04),
            "color": ("color", "ton", "egp", 1.0),
        },
        "electricity_per_ton_egp": 3233.8378874999994,
        "fixed_cost_per_kg_usd": 0.15238135851623189,
        "direct_labor_per_kg_usd": 0.0,
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
        "electricity_per_ton_egp": 4937.625783806608,
        "fixed_cost_per_kg_usd": 0.12505020582355653,
        "direct_labor_per_kg_usd": 0.023584587962962967,
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
    return _get_setting(conn, "dollar_rate", 45)


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


def compute_strap_line(conn, line_key, product, discount_pct=0, credit_term=False):
    """Full per-roll/per-kg breakdown for one strap_product row. Returns a
    dict with ex_work_price_roll, fob_price_roll/kg, cfr_price_roll/kg
    (Cash terms unless credit_term=True, in which case the flat $/kg
    surcharge is already included), plus gross_weight_kg (for converting
    a quantity of coils into total_kg elsewhere)."""
    cfg = LINE_CONFIG[line_key]
    bom = BOM_DEFS[line_key][product["bom_key"]]
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

    electricity_cost = roll_net_kg * cfg["electricity_per_ton_egp"] / 1000.0 / dollar_rate if dollar_rate else 0.0
    direct_labor_cost = roll_net_kg * cfg["direct_labor_per_kg_usd"]
    fixed_cost = roll_net_kg * cfg["fixed_cost_per_kg_usd"]
    ex_work_roll = material_cost + electricity_cost + direct_labor_cost + fixed_cost

    core_rate = _material_rate(conn, line_key, "core") / dollar_rate if dollar_rate else 0.0
    coil_price = ex_work_roll * (1 + bom["profit"]) + core_weight_kg * core_rate * (1 + PACKAGING_FACTORS["core"])

    packaging_total = _packaging_addons(
        conn, line_key, dollar_rate, core_weight_kg, bool(product["has_box"]), bool(product["has_pallet"])
    )

    if core_weight_kg > 0:
        ex_work_price_roll = _roundup2((packaging_total + coil_price) * (1 - (discount_pct or 0) / 100.0))
    else:
        ex_work_price_roll = 0.0

    fob_container_usd = _get_setting(conn, "strap_fob_cost_per_container_usd", 1100)
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
