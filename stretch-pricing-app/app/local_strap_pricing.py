"""Local Market PET Strap cost engine (v173).

Reverse-engineered directly from the owner's own PET_Local_pricing_1.14.xlsx
workbook ('Material cost'/'Fixed Cost'/'Electricity'/'PET' sheets), the same
way local_pricing.py was built from Stretch_Local_Pricing_H1.3.xlsx. This is
a brand-new system -- Local Market pricing never had a Strap module before
v173, only Stretch Film.

Every formula below was verified cell-for-cell against her sheet's own
"PET 15 x 1" (Manual, PET-Green) example row before shipping:
    meter_weight_g_per_m = 17.6726, roll_net_kg = 21.20712,
    material_cost = 749.68, electricity_cost = 68.58038814075898,
    fixed_cost = 64.39022570170155, ex_work_roll = 882.6506138424604,
    coil_price = 1059.1807366109524, packaging_total = 3.5723076923076924,
    ex_work_price_roll = 1062.76, selling_price_roll_cash = 1076.76,
    selling_price_kg_cash = 50.77351380102532,
    selling_price_roll_credit = 1111.751748,
    selling_price_kg_credit = 52.423513801025315
-- every one of those reproduced to the cent/fraction with the formulas here.

Deliberately simpler than Export's PET Strap (strap_pricing.py):
  - No FOB/CFR/container split -- a Local sale is EX-Work + a flat
    Transportation/Roll (her sheet's own Material cost!F4), not a per-
    container freight share.
  - No 30/60/90-day credit tiers -- her sheet's Credit column is one flat
    surcharge (0.03 x Dollar Rate, EGP/KG), not Export's tiered system.
  - No Automatic/Manual *line* split (that's Export's pet/pp line_key) --
    here Automatic/Manual is which BOM *recipe* is picked (it changes the
    BOM composition, profit % AND which Fixed Cost figure applies), exactly
    as her sheet models it.
  - No fixed catalog of SKUs -- a line is built from raw width/thickness/
    meters-per-coil inputs directly, same spirit as Export's own "Custom
    (width x thickness)" strap-line builder.

Everything here reads only 'local_strap_'-prefixed material_rate/
global_setting rows and the local_strap_bom table -- completely independent
from both Export's strap_pricing.py (strap_bom/strap_line_config/
material_rate without the local_strap_ prefix) and from Local's own Stretch
Film engine (local_pricing.py).
"""

import math

CORE_WEIGHT_THRESHOLD_KG = 0.7
CORE_FACTOR = 0.2  # her sheet's Factors!B21 (Core), flat 20% margin on core cost.
PACKAGING_FACTORS = {
    "jwan": 0.2,       # Factors!B26
    "stretch": 0.2,    # Factors!B22
    "box": 0.2,        # Factors!B25
    "cardboard": 0.2,  # Factors!B23
    "pallet": 0.2,     # Factors!B24
}


def _roundup2(x):
    """Excel's ROUNDUP(x, 2) -- always rounds away from zero, matching her
    sheet's own ROUNDUP() calls (never quote a fraction of a cent under)."""
    if x is None:
        return 0.0
    return math.ceil(x * 100 - 1e-9) / 100


def _get_setting(conn, key, default=0.0):
    row = conn.execute("SELECT value FROM global_setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None and row["value"] is not None else default


def _material_rate(conn, key):
    row = conn.execute("SELECT value FROM material_rate WHERE material_key=?", (key,)).fetchone()
    return row["value"] if row else 0.0


def get_recipes(conn):
    """All recipes (PET first, then PP -- v179), in a stable order."""
    return conn.execute(
        "SELECT * FROM local_strap_bom ORDER BY CASE line_key WHEN 'pet' THEN 1 ELSE 2 END, "
        "CASE recipe_key WHEN 'green_auto' THEN 1 WHEN 'colors' THEN 2 WHEN 'green_manual' THEN 3 ELSE 4 END, id"
    ).fetchall()


# v179 -- PP resin components: key -> (material_rate key, currency, extra
# multiplier). Same structure as Export's strap_pricing.LINE_CONFIG['pp']
# (5032 is USD/ton with the 1.035 scrap factor; the rest are EGP/ton).
PP_COMPONENTS = {
    "5032": ("local_strap_pp_5032", "usd", 1.035),
    "coco3": ("local_strap_pp_coco3", "egp", 1.0),
    "recycled_colored": ("local_strap_pp_recycled_colored", "egp", 1.0),
    "recycled_pure": ("local_strap_pp_recycled_pure", "egp", 1.0),
    "color": ("local_strap_pp_color", "egp", 1.0),
}


def pp_components(recipe):
    import json
    try:
        return json.loads(recipe["components_json"] or "{}")
    except Exception:
        return {}


def pp_meter_weight_g_per_m(width_mm, thickness_mm, components):
    """Export PP's own density-weighted formula (strap_pricing.meter_weight_g_per_m)."""
    if not (width_mm and thickness_mm):
        return 0.0
    coco3 = components.get("coco3", 0.0)
    other = (components.get("5032", 0.0) + components.get("recycled_colored", 0.0)
             + components.get("recycled_pure", 0.0))
    density = coco3 * 1.5 + other * 0.9
    return (width_mm - 0.2) * (thickness_mm * 0.7) * density


def _get_recipe(conn, recipe_key):
    row = conn.execute("SELECT * FROM local_strap_bom WHERE recipe_key=?", (recipe_key,)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM local_strap_bom ORDER BY id LIMIT 1").fetchone()
    return row


def meter_weight_g_per_m(width_mm, thickness_mm):
    """Her sheet's own flat PET formula (Material cost sheet, column P),
    identical in shape to Export's own PET formula in strap_pricing.py --
    both lines are the same PET resin, just priced independently."""
    if not (width_mm and thickness_mm):
        return 0.0
    if width_mm <= 12:
        return width_mm * (thickness_mm - 0.1) * 1.385
    return (width_mm - 0.5) * (thickness_mm - 0.12) * 1.385


def local_strap_capped_discount_pct(conn, line_discount_pct, global_discount_pct):
    """Her sheet's own flat 'Discount % (up to 4%)' cap -- a single combined
    ceiling, not per-category like Stretch Film's own Local discount caps.
    Returns (effective_pct, was_capped, max_allowed)."""
    max_allowed = _get_setting(conn, "local_strap_max_discount_pct", 4.0)
    requested = (line_discount_pct or 0) + (global_discount_pct or 0)
    effective = min(requested, max_allowed) if max_allowed else requested
    return effective, requested > max_allowed, max_allowed


def compute_local_strap_line(conn, recipe_key, width_mm, thickness_mm, meters_per_coil,
                              core_weight_kg=0, has_box=False, has_pallet=True,
                              discount_pct=0, credit_term=False):
    """Full per-roll/per-kg breakdown for one Local Strap line. Mirrors her
    sheet's PET tab columns D through AN exactly (see this module's
    docstring for the verified reference numbers)."""
    recipe = _get_recipe(conn, recipe_key)
    dollar_rate = _get_setting(conn, "local_strap_dollar_rate", 55)

    is_pp = recipe["line_key"] == "pp"
    if is_pp:
        pp_comps = pp_components(recipe)
        gm_per_m = pp_meter_weight_g_per_m(width_mm, thickness_mm, pp_comps)
    else:
        gm_per_m = meter_weight_g_per_m(width_mm, thickness_mm)
    roll_net_kg = (meters_per_coil or 0) * gm_per_m / 1000.0
    core_weight_kg = core_weight_kg or 0
    gross_weight_kg = roll_net_kg + core_weight_kg

    if is_pp:
        # -- v179 PP: same shape as Export PP (resin-mix BOM, + electricity,
        # fixed and direct labor per ton), priced in EGP --
        mat = 0.0
        for comp_key, frac in pp_comps.items():
            rate_key, currency, mult = PP_COMPONENTS[comp_key]
            rate = _material_rate(conn, rate_key)
            if currency == "usd":
                rate = rate * dollar_rate
            mat += roll_net_kg * frac * rate / 1000.0 * mult
        material_cost = _roundup2(mat * (1 + (recipe["waste_pct"] or 0)))
        electricity_cost = roll_net_kg * _get_setting(conn, "local_strap_pp_electricity_per_ton_egp", 0) / 1000.0
        fixed_cost = (roll_net_kg * _get_setting(conn, "local_strap_pp_fixed_cost_per_ton_egp", 0) / 1000.0
                      + roll_net_kg * _get_setting(conn, "local_strap_pp_direct_labor_per_ton_egp", 0) / 1000.0)
    else:
        # -- Material cost (BOM: PET + C4 + Color/Green-S66), her sheet's G:J --
        pet_rate = _material_rate(conn, "local_strap_pet")       # EGP/ton, no FX
        c4_rate_usd = _material_rate(conn, "local_strap_c4")      # USD/ton
        c4_rate_egp = c4_rate_usd * dollar_rate
        color_rate = _material_rate(conn, "local_strap_color")    # EGP/ton, no FX

        pet_cost = roll_net_kg * (recipe["pet_frac"] or 0) * pet_rate / 1000.0
        c4_cost = roll_net_kg * (recipe["c4_frac"] or 0) * c4_rate_egp / 1000.0 * 1.04
        color_cost = roll_net_kg * (recipe["color_frac"] or 0) * color_rate / 1000.0
        material_cost = _roundup2((pet_cost + c4_cost + color_cost) * (1 + (recipe["waste_pct"] or 0)))

        # -- Electricity + Fixed Cost, her sheet's K:L --
        electricity_per_ton = _get_setting(conn, "local_strap_electricity_per_ton_egp", 0)
        electricity_cost = roll_net_kg * electricity_per_ton / 1000.0

        if recipe["production_mode"] == "Manual":
            fixed_cost_per_kg = _get_setting(conn, "local_strap_fixed_cost_per_kg_manual", 0)
        else:
            fixed_cost_per_kg = _get_setting(conn, "local_strap_fixed_cost_per_kg_auto", 0)
        fixed_cost = roll_net_kg * fixed_cost_per_kg


    ex_work_roll = material_cost + electricity_cost + fixed_cost

    # -- Coil price: Ex-Work with profit margin + core raw material, her
    # sheet's X4 --
    core_rate_egp = _material_rate(conn, "local_strap_pp_core" if is_pp else "local_strap_core")
    coil_price = ex_work_roll * (1 + (recipe["profit_pct"] or 0)) + core_weight_kg * core_rate_egp * (1 + CORE_FACTOR)

    # -- Packaging add-ons (Jwan/Stretch/Box/Cardboard/Pallet), her sheet's
    # Y:AD -- jwan/box/cardboard/pallet only apply when there's a core
    # (T4>0 in her sheet); stretch-wrap always applies as long as there's a
    # roll at all (gated on gm_per_m>0, i.e. P4>0).
    jwan_rate = _material_rate(conn, "local_strap_jwan")
    stretch_rate = _material_rate(conn, "local_strap_stretch")
    box_rate = _material_rate(conn, "local_strap_box")
    cardboard_rate = _material_rate(conn, "local_strap_cardboard")
    pallet_rate = _material_rate(conn, "local_strap_pallet")
    small = core_weight_kg < CORE_WEIGHT_THRESHOLD_KG

    jwan = cardboard = box = pallet = 0.0
    if core_weight_kg > 0:
        jwan = jwan_rate * 2 * (1 + PACKAGING_FACTORS["jwan"])
        if has_box and small:
            box = (box_rate * (1 + PACKAGING_FACTORS["box"])) / 2
        elif has_box and not small:
            box = box_rate * (1 + PACKAGING_FACTORS["box"])
        if not has_box and has_pallet and small:
            cardboard = (cardboard_rate * 14 * (1 + PACKAGING_FACTORS["cardboard"])) / 66
        elif not has_box and has_pallet and not small:
            cardboard = (cardboard_rate * 14 * (1 + PACKAGING_FACTORS["cardboard"])) / 52
        if has_pallet and small and not has_box:
            pallet = (pallet_rate * (1 + PACKAGING_FACTORS["pallet"])) / 66
        elif has_pallet and small and has_box:
            pallet = (pallet_rate * (1 + PACKAGING_FACTORS["pallet"])) / 72
        elif has_pallet and not small:
            pallet = (pallet_rate * (1 + PACKAGING_FACTORS["pallet"])) / 52

    # v173 -- her sheet's Z4 formula treats T4=0 (no core at all) as the
    # SAME branch as "not small" (large core), not as "small" -- "small"
    # (the 66/33 divisors) only fires for a core that's present but under
    # the 0.7kg threshold. A coreless roll (the common case) always uses
    # the 52-divisor branch below.
    small_for_stretch = 0 < core_weight_kg < CORE_WEIGHT_THRESHOLD_KG
    stretch = 0.0
    if gm_per_m > 0:
        if small_for_stretch and not has_box:
            stretch = stretch_rate * 0.005 + stretch_rate * 1.2 / 66
        elif small_for_stretch and has_box:
            stretch = stretch_rate * 0.005 + stretch_rate * 1.2 / 33
        else:  # core_weight_kg == 0, or core >= 0.7kg (box or no box alike)
            stretch = stretch_rate * 0.01 + stretch_rate * 1.2 / 52
        stretch *= (1 + PACKAGING_FACTORS["stretch"])

    packaging_total = jwan + stretch + box + cardboard + pallet

    # -- Ex-Work price (her sheet's own flat discount on the WHOLE total,
    # not just the profit margin -- replicated exactly as her formula, the
    # 4%-cap keeps this from ever eating meaningfully into real cost) --
    if gm_per_m > 0:
        ex_work_price_roll = _roundup2((packaging_total + coil_price) * (1 - (discount_pct or 0) / 100.0))
    else:
        ex_work_price_roll = 0.0

    transport_per_roll = _get_setting(conn, "local_strap_transport_per_roll_egp", 0)
    selling_price_roll_cash = ex_work_price_roll + transport_per_roll if ex_work_price_roll > 0 else 0.0
    selling_price_kg_cash = selling_price_roll_cash / gross_weight_kg if gross_weight_kg else 0.0

    selling_price_roll = selling_price_roll_cash
    selling_price_kg = selling_price_kg_cash
    if credit_term and selling_price_roll_cash > 0:
        surcharge_factor = _get_setting(conn, "local_strap_credit_surcharge_factor", 0.03)
        selling_price_kg = selling_price_kg_cash + surcharge_factor * dollar_rate
        selling_price_roll = selling_price_kg * gross_weight_kg

    return {
        "recipe_label": recipe["label"] if recipe else "-",
        "line_key": recipe["line_key"] if recipe else "pet",
        "gm_per_m": gm_per_m,
        "roll_net_kg": roll_net_kg,
        "gross_weight_kg": gross_weight_kg,
        "material_cost": material_cost,
        "electricity_cost": electricity_cost,
        "fixed_cost": fixed_cost,
        "ex_work_roll": ex_work_roll,
        "coil_price": coil_price,
        "packaging_total": packaging_total,
        "ex_work_price_roll": ex_work_price_roll,
        "selling_price_roll_cash": selling_price_roll_cash,
        "selling_price_kg_cash": selling_price_kg_cash,
        "selling_price_roll": selling_price_roll,
        "selling_price_kg": selling_price_kg,
    }
