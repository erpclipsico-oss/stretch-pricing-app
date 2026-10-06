"""Local Pricing System -- Stretch Film (Phase 1).

Owner's big 2026-10-04 Arabic spec, item 4: a Local Pricing System for
Stretch Film / PET Strap / PP Strap, "with a different backend ... own
recipe/BOM/factors", EX-Work + local transport = Selling Price (no FOB/CIF/
container at all), priced in EGP (confirmed against her own
Stretch_Local_Pricing_H1.3 workbook's "Stretch" sheet: 'Cash (EGP)' /
'Credit (EGP)' selling-price columns), admin/sub_admin/user roles with
their own per-seller markup (user.local_markup_mode/value -- see db.py's
matching v156 migration). This module covers Stretch Film only; PET/PP
Strap Local is a later phase (same pattern, its own local_strap_* tables,
not yet built -- see app.py's local-system routes).

Deliberately reuses cost_engine's EXISTING functions for the three pieces
of cost that are genuine shared-factory overhead, identical whether a roll
is sold Export or Local (same machines, same electricity bill, same labor,
same packaging quantities): core_cost_usd(), packaging_cost_per_roll_usd()
(and everything under it -- pallet_component_total_usd,
effective_rolls_per_pallet, lookup_packing_tier) and
conversion_cost_usd_per_ton(). Those still read Export's own (un-prefixed)
material_rate rows for packaging materials (cap/pallet/cardboard/etc) and
Export's own labor/electricity/fixed-cost tables -- a deliberate
simplification, since those represent the same physical factory overhead
regardless of which market a given roll is sold into, not a
market-dependent price. Only the RAW RESIN cost (C4/Exceed3518/Exceed3812/
ExceedXP/Vista6000/Enable/LD/Vista/UVI) and the margin % use Local's own
independent data (local_-prefixed material_rate rows, local_bom_row,
local_margin_factor) -- those are the pieces the owner explicitly asked to
be independently priced.
"""

from . import cost_engine


# ---------------------------------------------------------------- settings/material rate helpers

def _local_setting(conn, key, default=0.0):
    row = conn.execute("SELECT value FROM global_setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None and row["value"] is not None else default


def _local_material_rate(conn, key, default=0.0):
    row = conn.execute("SELECT value FROM material_rate WHERE material_key=?", ("local_" + key,)).fetchone()
    return row["value"] if row is not None and row["value"] is not None else default


# ---------------------------------------------------------------- BOM / material composition (Local's own recipe table)

def get_local_bom_row(conn, stretch_multiplier, micron, roll_tier):
    """Exact mirror of cost_engine.get_bom_row()'s fallback logic (exact
    match, else nearest micron for that multiplier/tier, else nearest
    multiplier too), against local_bom_row instead of bom_row."""
    row = conn.execute(
        "SELECT * FROM local_bom_row WHERE stretch_multiplier=? AND micron=? AND roll_tier=?",
        (stretch_multiplier, micron, roll_tier),
    ).fetchone()
    if row:
        return row
    candidates = conn.execute(
        "SELECT * FROM local_bom_row WHERE stretch_multiplier=? AND roll_tier=?",
        (stretch_multiplier, roll_tier),
    ).fetchall()
    if not candidates:
        candidates = conn.execute("SELECT * FROM local_bom_row WHERE roll_tier=?", (roll_tier,)).fetchall()
    if not candidates:
        return None
    return min(candidates, key=lambda r: abs((r["micron"] or 0) - (micron or 0)))


def local_material_composition(conn, product, uv_fraction=0.0):
    """Same shape/logic as cost_engine.material_composition(), but reads
    Local's own local_bom_row table instead of Export's bom_row -- see that
    function's docstring for the REGID Film / Super REGID Film hardcoded
    special case, reproduced identically here since it's pure product
    geometry, not a market-dependent price."""
    roll_weight = product["roll_weight_kg"] or 0
    roll_tier = "jumbo" if roll_weight > 25 else "standard"
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0

    stretch_ability = product["stretch_ability"] or ""
    if stretch_ability in ("REGID Film", "Super REGID Film"):
        if stretch_ability == "Super REGID Film":
            enable = 0.5 if micron <= 10 else 0.4
        else:
            enable = 0.4 if micron <= 10 else 0.3
        comp = {
            "exceed3518": 0.0, "exceed3812": 0.0, "exceedxp": 0.0, "vista6000": 0.0,
            "enable": enable, "ld": 0.0, "vista": 0.007,
        }
        comp["uvi"] = uv_fraction or 0.0
        return comp

    mult = cost_engine.bom_stretch_multiplier(stretch_ability)
    bom = get_local_bom_row(conn, mult, micron, roll_tier)
    if bom is None:
        comp = {k: 0.0 for k in ["exceed3518", "exceed3812", "exceedxp", "vista6000", "enable", "ld", "vista"]}
    else:
        comp = {
            "exceed3518": bom["exceed3518"] or 0, "exceed3812": bom["exceed3812"] or 0,
            "exceedxp": bom["exceedxp"] or 0, "vista6000": bom["vista6000"] or 0,
            "enable": bom["enable"] or 0, "ld": bom["ld258"] or 0, "vista": bom["vista6202"] or 0,
        }
    comp["uvi"] = uv_fraction or 0.0
    return comp


def compute_local_ex_work_usd_kg(conn, product, pallet_type=None, rolls_per_pallet_override=None,
                                   uv_fraction=0.0, box_packaging=True, slippery=False):
    """Local's own EX-Work $/KG (still USD at this stage -- converted to
    EGP only once, in local_unit_price_for(), the same point Export's own
    EGP-priced packaging items get converted the other way). Mirrors
    cost_engine.compute_ex_work_usd_kg() exactly, except: (a) raw-resin
    rates come from _local_material_rate()/local_material_composition()
    (Local's own independent prices/recipe) instead of Export's, (b) waste/
    scrap/material-interest factors come from Local's own local_waste_factor
    etc settings, and (c) core cost / packaging cost / conversion cost
    (other_costs) are Export's shared-factory functions, called as-is --
    see this module's docstring for why."""
    roll_weight = product["roll_weight_kg"] or 0
    core_weight = product["core_weight_kg"] or 0
    plastic_weight = max(roll_weight - core_weight, 0)
    if roll_weight <= 0:
        return 0.0

    waste = _local_setting(conn, "local_waste_factor", 1.01)
    scrap = _local_setting(conn, "local_scrap_interest_factor", 1.04)

    comp = local_material_composition(conn, product, uv_fraction=uv_fraction)
    c4_fraction = max(1 - sum(comp.values()), 0)

    def mat_cost(key, fraction):
        rate = _local_material_rate(conn, key)
        return fraction * plastic_weight * rate / 1000 * waste * scrap

    material_cost = mat_cost("c4", c4_fraction)
    material_cost += mat_cost("exceed3518", comp["exceed3518"])
    material_cost += mat_cost("exceed3812", comp["exceed3812"])
    material_cost += mat_cost("exceedxp", comp["exceedxp"])
    material_cost += mat_cost("vista6000", comp["vista6000"])
    material_cost += mat_cost("enable", comp["enable"])
    material_cost += mat_cost("ld", comp["ld"])
    material_cost += mat_cost("vista", comp["vista"])
    material_cost += mat_cost("uvi", comp.get("uvi", 0.0))
    if slippery:
        # v180 -- Extra Slippery (Local's own rate/dosage): dosage% x plastic weight x EGP/kg / Local dollar rate
        material_cost += (_local_setting(conn, "local_slippery_dosage_pct", 0.6) / 100.0 * plastic_weight
                          * _local_material_rate(conn, "slippery") / (_local_setting(conn, "local_dollar_rate", 52) or 52))

    # v170.1 -- Local's own independent core/packaging/conversion costs
    # (see cost_engine.py's "Local Market's own independent core/packaging/
    # conversion costs" section) -- no longer Export's shared-factory
    # functions (owner-requested: "زي ما انا بعتها لك بالظبط لللوكل ماركت
    # بس"). box_packaging is accepted for call-signature compatibility with
    # Export's own packaging function but has no effect here -- Local's
    # own Pallet component sheet never modeled a no-box variant.
    core_cost = cost_engine.local_core_cost_usd(conn, product)
    packaging_cost = (cost_engine.local_packaging_cost_per_roll_usd(conn, product, pallet_type,
                                                                      rolls_per_pallet_override)
                       if plastic_weight > 0 else 0.0)

    interest_rate = _local_setting(conn, "local_material_interest_rate", 0.0)
    material_interest = material_cost * interest_rate

    roll_type = cost_engine.conversion_roll_type_for(product["stretch_ability"], product["micron"])
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0
    conv_usd_per_ton = cost_engine.local_conversion_cost_usd_per_ton(conn, micron, roll_type)
    width_mm = product["width_mm"] if "width_mm" in product.keys() else None
    width = width_mm or 500
    width_factor = (500 / width) if width and width < 500 else 1
    other_costs = plastic_weight * conv_usd_per_ton / 1000 * width_factor

    total = material_cost + core_cost + packaging_cost + material_interest + other_costs
    return round(total / roll_weight, 4)


# ---------------------------------------------------------------- margin (Local's own Customer Class A/B table)

def local_margin_category_for(conn, product, auto_manual, uv=False):
    """Maps a Local line onto one of local_margin_factor's 7 categories,
    per the owner's own Factors sheet (see db.LOCAL_MARGIN_FACTOR_ROWS):
    'auto_8_9' / 'auto_10_12' / 'auto_other' / 'manual' / 'uvi' / 'rigid' /
    'uv_rigid'. Returns (category, film_type) -- film_type is None for
    every category except auto_10_12/auto_other, which split by Standard/
    Power/Power_Plus (cost_engine.FILM_TYPE_FOR_ROLL_TYPE's own strings)."""
    roll_type = cost_engine.roll_type_bucket(product["stretch_ability"])
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0
    is_manual = "manual" in (auto_manual or "").lower()

    if roll_type == "RIGID":
        return ("uv_rigid" if uv else "rigid"), None
    if uv:
        return "uvi", None
    if micron in (8, 9):
        return "auto_8_9", None
    if is_manual:
        return "manual", None
    film_type = cost_engine.FILM_TYPE_FOR_ROLL_TYPE.get(roll_type, "Standard")
    if micron in (10, 12):
        return "auto_10_12", film_type
    return "auto_other", film_type


def local_margin_pct_for(conn, product, auto_manual, uv=False, customer_class="A"):
    category, film_type = local_margin_category_for(conn, product, auto_manual, uv=uv)
    if film_type is None:
        row = conn.execute(
            "SELECT margin_pct FROM local_margin_factor WHERE customer_class=? AND category=? AND film_type IS NULL",
            (customer_class, category),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT margin_pct FROM local_margin_factor WHERE customer_class=? AND category=? AND film_type=?",
            (customer_class, category, film_type),
        ).fetchone()
    if row is None:
        return 0.0
    return (row["margin_pct"] or 0.0) / 100.0


# ---------------------------------------------------------------- unit price (EGP/KG)

def local_unit_price_for(db, product, customer_class="A", pallet_type=None, rolls_per_pallet_override=None,
                           auto_manual_override=None, colored=False, uv=False, discount_pct=0,
                           payment_term="Cash", hidden_markup_mode=None, hidden_markup_value=0,
                           destination=None, round_result=True, margin_pct_override=None,
                           box_packaging=True, slippery=False):
    """Local's own EX-Work + margin -> EGP -> + Transportation = Selling
    Price EGP/KG. See this module's docstring for the overall design."""
    from .pricing import _discounted_factor  # local import: avoids a pricing.py <-> local_pricing.py cycle

    effective_product = dict(product)
    if auto_manual_override not in (None, ""):
        effective_product["auto_manual"] = auto_manual_override
    auto_manual = effective_product["auto_manual"] or ""

    if margin_pct_override is not None:
        factor = margin_pct_override
    else:
        factor = local_margin_pct_for(db, effective_product, auto_manual, uv=uv, customer_class=customer_class)
        factor = _discounted_factor(factor, discount_pct)

    uv_fraction = cost_engine.UVI_FRACTION if uv else 0.0
    ex_work_usd = compute_local_ex_work_usd_kg(db, effective_product, pallet_type=pallet_type,
                                                rolls_per_pallet_override=rolls_per_pallet_override,
                                                uv_fraction=uv_fraction, box_packaging=box_packaging,
                                                slippery=slippery)
    selling_usd = ex_work_usd * (1 + factor)

    dollar_rate = _local_setting(db, "local_dollar_rate", 52)
    price_egp = selling_usd * dollar_rate

    if colored:
        price_egp += _local_setting(db, "local_extra_color_egp_kg", 0)
    price_egp = cost_engine.apply_hidden_markup(price_egp, hidden_markup_mode, hidden_markup_value)
    if payment_term == "Credit":
        price_egp += _local_setting(db, "local_extra_credit_egp_kg", 0)

    transport_egp_kg = 0.0
    if destination:
        row = db.execute("SELECT transport_egp_kg FROM local_destination WHERE name=?", (destination,)).fetchone()
        transport_egp_kg = (row["transport_egp_kg"] or 0.0) if row else 0.0
    price_egp += transport_egp_kg

    return cost_engine.round_half_up(price_egp, 2) if round_result else price_egp


def compute_local_line(db, product, customer_class, quantity_pallets, pallet_type=None,
                         rolls_per_pallet_override=None, auto_manual_override=None, colored=False, uv=False,
                         discount_pct=0, payment_term="Cash", hidden_markup_mode=None, hidden_markup_value=0,
                         destination=None, roll_weight_kg=None, core_weight_kg=None, width_mm=None,
                         box_packaging=True, margin_pct_override=None, slippery=False):
    """Quantity/weight bookkeeping wrapper, same shape as pricing.compute_line()."""
    effective_product = cost_engine.with_overrides(product, roll_weight_kg, core_weight_kg, width_mm,
                                                     auto_manual=auto_manual_override)
    unit_price = local_unit_price_for(
        db, effective_product, customer_class=customer_class, pallet_type=pallet_type,
        rolls_per_pallet_override=rolls_per_pallet_override, colored=colored, uv=uv,
        discount_pct=discount_pct, payment_term=payment_term, hidden_markup_mode=hidden_markup_mode,
        hidden_markup_value=hidden_markup_value, destination=destination, box_packaging=box_packaging,
        margin_pct_override=margin_pct_override, slippery=slippery,
    )
    rolls_per_pallet = cost_engine.effective_rolls_per_pallet(db, effective_product, pallet_type,
                                                                rolls_per_pallet_override)
    total_kg = (effective_product["roll_weight_kg"] or 0) * rolls_per_pallet * (quantity_pallets or 0)
    return unit_price, total_kg
