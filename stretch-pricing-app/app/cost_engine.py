"""Cost engine: computes EX-Work $/KG from the raw, editable cost tables.

This module is a faithful (documented) re-implementation of the Excel
workbook's 'Stretch' master sheet formula chain, pulling from:
  Material pricing, اجور مباشرة (direct labor), Electricity,
  تكاليف متغيرة (variable costs), تكاليف ثابتة (fixed costs),
  Conversion cost, Pallet component, BOM.

See app/COST_ENGINE.md for the full write-up of the formula chain, the
assumptions made, and the two places where the source workbook's intent
had to be interpreted (the 1.01 / 1.031 material multipliers, and the
jumbo/pre-stretch roll-weight special cases).

Everything here is READ-ONLY with respect to the database: it takes a
sqlite3 connection and a product row and returns computed numbers. The
admin screens are the only place raw tables are edited; this module is
called fresh every time a price is needed, so edits take effect
immediately (no caching/staleness within a request).
"""

import re


# ---------------------------------------------------------------- helpers

def _get_setting(conn, key, default=0.0):
    row = conn.execute("SELECT value FROM global_setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None and row["value"] is not None else default


def _material_rate(conn, key, default=0.0):
    row = conn.execute("SELECT value FROM material_rate WHERE material_key=?", (key,)).fetchone()
    return row["value"] if row is not None and row["value"] is not None else default


def with_overrides(product, roll_weight_kg=None, core_weight_kg=None, width_mm=None, auto_manual=None):
    """Returns a plain dict standing in for a product row, with roll weight /
    core weight / width overridden by the values a quotation line actually
    entered (these vary per customer order -- see COST_ENGINE.md v9 note --
    so the catalog's roll_weight_kg/core_weight_kg/width_mm are only
    defaults). A dict works everywhere a sqlite3.Row is used below since
    every accessor here does plain product[...] indexing / product.keys().
    roll_weight_kg and width_mm are only overridden when a positive number
    is given (0/blank means "use the catalog value"); core_weight_kg of 0 is
    a valid, meaningful override (no core) so it's only skipped when the
    value is missing entirely.

    auto_manual (v21 bug fix): the quotation LINE's own "Packing type"
    choice (Automatic / Manual(5kg) / Manual(2.3~3.5kg) / Manual(2.2kg) /
    Manual(1.5kg)) now overrides the product catalog's auto_manual for
    costing -- margin_pct_for(), packaging_cost_per_roll_usd() and
    effective_rolls_per_pallet() all read product["auto_manual"], so this
    is the single place that override has to land. Previously the rep's
    per-line Packing type selection was only ever stored on the saved
    quotation_line row for display and never actually affected the price,
    which always priced strictly off the selected product's own catalog
    Automatic/Manual value -- reported by the owner as a bug (comparing to
    the reference app, where changing Packing type visibly changes the
    price) and fixed here."""
    d = dict(product)
    if roll_weight_kg not in (None, ""):
        try:
            v = float(roll_weight_kg)
            if v > 0:
                d["roll_weight_kg"] = v
        except (TypeError, ValueError):
            pass
    if core_weight_kg not in (None, ""):
        try:
            d["core_weight_kg"] = max(float(core_weight_kg), 0)
        except (TypeError, ValueError):
            pass
    if width_mm not in (None, ""):
        try:
            v = float(width_mm)
            if v > 0:
                d["width_mm"] = v
        except (TypeError, ValueError):
            pass
    if auto_manual not in (None, ""):
        d["auto_manual"] = auto_manual
    return d


ROLL_TYPES = ["St", "P", "P_plus", "RIGID"]


def roll_type_bucket(stretch_ability):
    """Map a product's free-text Stretch Ability (e.g. '300% (Power plus)',
    'REGID Film') onto the physical machine/electricity bucket used by the
    Electricity / Conversion cost sheets: St, P, P_plus or RIGID."""
    s = (stretch_ability or "").lower()
    if "regid" in s or "rigid" in s:
        return "RIGID"
    if "power plus" in s or "power+" in s or "power_plus" in s:
        return "P_plus"
    if "power" in s:
        return "P"
    return "St"


def bom_stretch_multiplier(stretch_ability):
    """Extract the BOM sheet's 'Stretch Ability' multiplier (1.5, 2, 2.5, 3,
    3.5) from text like '150% Standard' or '350% (Power plus)'. Falls back
    to 1.5 (the most common / lowest tier) if no percentage is found."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", stretch_ability or "")
    if m:
        return round(float(m.group(1)) / 100, 2)
    return 1.5


# ---------------------------------------------------------------- BOM

def get_bom_row(conn, stretch_multiplier, micron, roll_tier):
    """Exact (multiplier, micron, tier) match, else nearest micron for that
    multiplier/tier, else nearest multiplier too. Mirrors the workbook's
    assumption that every stretch-ability/micron combination has its own
    BOM row; falling back to the closest defined row is a documented
    approximation for combinations the workbook itself never listed."""
    row = conn.execute(
        "SELECT * FROM bom_row WHERE stretch_multiplier=? AND micron=? AND roll_tier=?",
        (stretch_multiplier, micron, roll_tier),
    ).fetchone()
    if row:
        return row
    candidates = conn.execute(
        "SELECT * FROM bom_row WHERE stretch_multiplier=? AND roll_tier=?",
        (stretch_multiplier, roll_tier),
    ).fetchall()
    if not candidates:
        candidates = conn.execute("SELECT * FROM bom_row WHERE roll_tier=?", (roll_tier,)).fetchall()
    if not candidates:
        return None
    return min(candidates, key=lambda r: abs((r["micron"] or 0) - (micron or 0)))


def material_composition(conn, product):
    """Returns dict of material_key -> weight fraction (0..1) for this
    product, pulled from the BOM table using roll-weight threshold (25kg)
    exactly like Stretch!L3:S3 (`IF(H3>25, jumbo column, standard column)`)."""
    roll_weight = product["roll_weight_kg"] or 0
    roll_tier = "jumbo" if roll_weight > 25 else "standard"
    mult = bom_stretch_multiplier(product["stretch_ability"])
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0
    bom = get_bom_row(conn, mult, micron, roll_tier)
    if bom is None:
        return {k: 0.0 for k in ["exceed3518", "exceed3812", "exceedxp", "vista6000", "enable", "ld", "vista"]}
    comp = {
        "exceed3518": bom["exceed3518"] or 0,
        "exceed3812": bom["exceed3812"] or 0,
        "exceedxp": bom["exceedxp"] or 0,
        "vista6000": bom["vista6000"] or 0,
        "enable": bom["enable"] or 0,
        "ld": bom["ld258"] or 0,
        "vista": bom["vista6202"] or 0,
    }
    return comp


# ---------------------------------------------------------------- Electricity / Conversion cost

def _lookup_kw_per_ton(conn, micron, roll_type):
    row = conn.execute(
        "SELECT kw_per_ton FROM electricity_power WHERE micron=? AND roll_type=?", (micron, roll_type)
    ).fetchone()
    if row:
        return row["kw_per_ton"] or 0
    rows = conn.execute(
        "SELECT * FROM electricity_power WHERE roll_type=?", (roll_type,)
    ).fetchall()
    if not rows:
        return 0
    nearest = min(rows, key=lambda r: abs((r["micron"] or 0) - micron))
    return nearest["kw_per_ton"] or 0


def _lookup_tons_per_day(conn, micron, roll_type):
    row = conn.execute(
        "SELECT tons_per_day FROM production_capacity WHERE micron=? AND roll_type=?", (micron, roll_type)
    ).fetchone()
    if row:
        return row["tons_per_day"] or 0
    rows = conn.execute(
        "SELECT * FROM production_capacity WHERE roll_type=?", (roll_type,)
    ).fetchall()
    if not rows:
        return 0
    nearest = min(rows, key=lambda r: abs((r["micron"] or 0) - micron))
    return nearest["tons_per_day"] or 0


def total_fixed_cost_egp(conn):
    row = conn.execute("SELECT COALESCE(SUM(value_egp),0) t FROM fixed_cost_item").fetchone()
    return row["t"] or 0


def conversion_cost_usd_per_ton(conn, micron, roll_type):
    """Replicates: Electricity (kW/ton x EGP/kWh) + small variable-cost
    overhead items -> variable cost/ton (EGP); total fixed cost / monthly
    production tons -> fixed cost/ton (EGP); sum, then /dollar_rate ->
    USD/ton. This is the 'Depreciation + D.labor + Machine Power (24kw/ton)'
    figure the Stretch sheet's AF79:AF85 comment names, i.e. the
    Conversion Cost."""
    capacity_pct = _get_setting(conn, "capacity_usage_pct", 0.8)
    dollar_rate = _get_setting(conn, "dollar_rate", 45)
    variable_tariff = _get_setting(conn, "electricity_variable_tariff_egp_per_kwh", 1.32)

    kw_per_ton = _lookup_kw_per_ton(conn, micron, roll_type)
    tons_per_day = _lookup_tons_per_day(conn, micron, roll_type)
    monthly_tons = tons_per_day * capacity_pct * 30

    variable_electricity_egp_per_ton = round(kw_per_ton * variable_tariff)  # ROUNDUP in sheet; round is close enough
    extra_items = conn.execute("SELECT COALESCE(SUM(value_egp_per_ton),0) t FROM variable_cost_item").fetchone()["t"] or 0
    variable_cost_per_ton_egp = variable_electricity_egp_per_ton + extra_items

    fixed_total = total_fixed_cost_egp(conn)
    fixed_cost_per_ton_egp = (fixed_total / monthly_tons) if monthly_tons else 0

    total_egp_per_ton = variable_cost_per_ton_egp + fixed_cost_per_ton_egp
    return total_egp_per_ton / dollar_rate if dollar_rate else 0


# ---------------------------------------------------------------- Pallet component

def get_pallet_component(conn, packing_key):
    return conn.execute("SELECT * FROM pallet_component WHERE packing_key=?", (packing_key,)).fetchone()


def pallet_component_total_usd(conn, packing_key):
    """Sums a Pallet component variant's line items at current material
    rates, mirroring e.g. 'Pallet component'!D11 = SUM(D6:D10)."""
    pc = get_pallet_component(conn, packing_key)
    if pc is None:
        return 0.0
    dollar_rate = _get_setting(conn, "dollar_rate", 45)
    if not dollar_rate:
        return 0.0
    total = 0.0
    total += (pc["pallet_qty"] or 0) * _material_rate(conn, "pallet") / dollar_rate
    total += (pc["cardboard_qty"] or 0) * _material_rate(conn, "cardboard") / dollar_rate
    total += (pc["cap_qty"] or 0) * _material_rate(conn, "cap") / dollar_rate
    total += (pc["corrugated_kg"] or 0) * _material_rate(conn, "corrugated_sheets") / dollar_rate
    total += (pc["stretch_kg"] or 0) * _material_rate(conn, "stretch") / dollar_rate
    total += (pc["box_qty"] or 0) * _material_rate(conn, "box") / dollar_rate
    total += (pc["cartoon_angle_qty"] or 0) * _material_rate(conn, "cartoon_angle") / dollar_rate
    total += (pc["scotch_tape_qty"] or 0) * _material_rate(conn, "scotch_tape") / dollar_rate
    if "air_bag_qty" in pc.keys():
        total += (pc["air_bag_qty"] or 0) * _material_rate(conn, "air_bag") / dollar_rate
    if "pe_bag_qty" in pc.keys():
        total += (pc["pe_bag_qty"] or 0) * _material_rate(conn, "pe_bag") / dollar_rate
    return total


def _pallet_key_for(auto_manual, pallet_size, packaging_group=None):
    """auto_manual: 'Automatic' | 'Manual(5kg)' | 'Manual(2.3~3.5kg)' |
    'Manual(2.2kg)' | 'Manual(1.5kg)'. pallet_size: 'Standard' (USD/120x100)
    or 'Euro' (EUR/120x80). packaging_group: a product-level override (see
    product.packaging_group / db.py's _seed_box_packaging_v3) that bypasses
    the normal Automatic/Manual lookup entirely -- e.g. 12-micron 300%
    film, which is boxed (roll -> PE bag -> box) rather than packed the
    standard Automatic way, regardless of its auto_manual value."""
    is_eur = "euro" in (pallet_size or "").lower()
    suffix = "eur" if is_eur else "usd"
    if packaging_group:
        return f"{packaging_group}_{suffix}"
    am = (auto_manual or "Automatic").lower()
    if "manual" in am:
        if "5kg" in am or "5 kg" in am:
            return f"manual_smallbox5_{suffix}"
        if "2.3" in am or "3.5" in am:
            return f"manual_largebox_{suffix}"
        if "2.2" in am:
            return f"manual_smallbox22_{suffix}"
        if "1.5" in am:
            return f"manual_smallbox15_{suffix}"
        return f"manual_smallbox5_{suffix}"
    return f"automatic_{suffix}"


def lookup_packing_tier(conn, auto_manual, roll_weight_kg, pallet_type=None):
    """Match a product's Auto/Manual + actual roll weight (and pallet type,
    Standard vs Euro) onto the Details-sheet packing tier: rolls/box,
    box/pallet, rolls/pallet, pallets/container(40'/20'). Uses an exact
    category+pallet_type match, then the *nearest* match_weight_kg within
    that category+pallet_type (there is no ambiguity for the seeded tiers,
    since each category's weight buckets are spaced well apart, but nearest-
    match is used rather than requiring an exact hit so a product's actual
    roll weight -- e.g. 16.0 vs a 16 kg tier -- doesn't need to match to the
    decimal)."""
    category = "Manual" if "manual" in (auto_manual or "").lower() else "Automatic"
    is_euro = "euro" in (pallet_type or "").lower()
    p_type = "Euro" if is_euro else "Standard"
    rows = conn.execute(
        "SELECT * FROM packing_tier WHERE category=? AND pallet_type=?", (category, p_type)
    ).fetchall()
    if not rows:
        rows = conn.execute("SELECT * FROM packing_tier WHERE category=?", (category,)).fetchall()
    if not rows:
        return None
    weight = roll_weight_kg or 0
    return min(rows, key=lambda r: abs((r["match_weight_kg"] or 0) - weight))


def effective_rolls_per_pallet(conn, product, pallet_type=None, rolls_per_pallet_override=None):
    """Rolls/pallet for costing and quote totals: an explicit per-line
    override (the rep typed in a known rolls/pallet count for a non-catalog
    roll weight) wins outright; otherwise prefers the Details-sheet
    packing_tier match (by the product's actual roll weight + the quote
    line's chosen pallet type), falling back to the product's own manually-
    entered rolls_per_pallet field when no tier matches (e.g. a product with
    no roll weight set yet)."""
    if rolls_per_pallet_override:
        try:
            v = float(rolls_per_pallet_override)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    tier = lookup_packing_tier(conn, product["auto_manual"], product["roll_weight_kg"], pallet_type)
    if tier and tier["rolls_per_pallet"]:
        return tier["rolls_per_pallet"]
    return product["rolls_per_pallet"] or 0


def packaging_cost_per_roll_usd(conn, product, pallet_type=None, rolls_per_pallet_override=None):
    """Stretch!AD column: automatic/manual packaging cost divided by rolls
    per pallet, with the jumbo (>25kg) special case that excludes the base
    Pallet line item (mirrors 'Pallet component'!(D11-D6)/G on row 40).
    Rolls/pallet now comes from the Details-sheet packing_tier lookup
    (exact gross-weight bucket x pallet type), not a single hardcoded
    per-product value -- see lookup_packing_tier()."""
    rolls_per_pallet = effective_rolls_per_pallet(conn, product, pallet_type, rolls_per_pallet_override)
    if rolls_per_pallet <= 0:
        return 0.0
    packaging_group = product["packaging_group"] if "packaging_group" in product.keys() else None
    key = _pallet_key_for(product["auto_manual"], pallet_type or product["pallet_size"], packaging_group)
    total = pallet_component_total_usd(conn, key)
    roll_weight = product["roll_weight_kg"] or 0
    if roll_weight > 25 and key.startswith("automatic"):
        # Jumbo rolls: exclude the base pallet unit cost (workbook subtracts
        # 'Pallet component'!D6 / J6, the bare "Pallet" line, from the total
        # before dividing by rolls/pallet).
        dollar_rate = _get_setting(conn, "dollar_rate", 45)
        pallet_rate = _material_rate(conn, "pallet") / dollar_rate if dollar_rate else 0
        total = max(total - pallet_rate, 0)
    return total / rolls_per_pallet


# ---------------------------------------------------------------- Core / material interest

def core_cost_usd(conn, product):
    dollar_rate = _get_setting(conn, "dollar_rate", 45)
    core_rate = _material_rate(conn, "core")
    core_weight = product["core_weight_kg"] or 0
    if not dollar_rate:
        return 0.0
    return core_weight * (core_rate / dollar_rate)


# ------------------------------------------------------------ Margin factor (v18)

# roll_type_bucket() -> margin_factor.film_type. RIGID has no Regular/Super
# distinction in this catalog (the app has no field to tell them apart), but
# Regular_Rigid and Super_Rigid carry IDENTICAL margin numbers in the seeded
# table (see db.MARGIN_FACTOR_ROWS), so mapping every RIGID product onto
# Regular_Rigid is lossless -- it will always return the same margin% Super_Rigid
# would have, for every micron/packing/roll-size combination.
FILM_TYPE_FOR_ROLL_TYPE = {
    "St": "Standard",
    "P": "Power",
    "P_plus": "Power_Plus",
    "RIGID": "Regular_Rigid",
}


def margin_pct_for(conn, product, pallet_type=None, rolls_per_pallet_override=None,
                    prestretch_packaging_type=None):
    """Replaces the old country_class x customer_class x roll_size `factor`
    lookup with the reference app's micron x film_type x packing_type x
    roll_size `margin_factor` lookup. Returns the margin as a FRACTION
    (e.g. 0.13 for 13%), matching what pricing.unit_price_for()'s
    `ex_work * (1 + factor)` formula expects -- margin_factor.margin_pct is
    stored as a raw percentage (13.00), so this divides by 100.

    `product` may be a plain dict (e.g. from with_overrides()) or a
    sqlite3.Row; only plain product[...] indexing / product.keys() is used,
    same convention as the rest of this module.

    Pre-Stretch products (product['is_prestretch']) map to film_type
    'Prestretch', with packing_type 'Pre-stretch (Box)' when
    prestretch_packaging_type == 'boxes', else 'Pre-stretch (No Box)'.

    No current catalog product needs a UVI_* or UV_Rigid film_type (no
    UV-protected product exists in the catalog today) -- that data is
    seeded for completeness/future use only; this lookup never derives
    those film_types for a real product, so they simply never match.

    If no row matches (should not happen for any current catalog product
    given the seeded ranges), falls back to 0% margin rather than raising --
    note this in any report if it's ever observed firing for a real
    product, since it would silently sell at EX-Work with no markup.
    """
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0

    is_prestretch = bool(product["is_prestretch"]) if "is_prestretch" in product.keys() else False
    if is_prestretch:
        film_type = "Prestretch"
        packing_type = "Pre-stretch (Box)" if prestretch_packaging_type == "boxes" else "Pre-stretch (No Box)"
        roll_size = "Prestretch Roll size"
    else:
        roll_type = roll_type_bucket(product["stretch_ability"])
        film_type = FILM_TYPE_FOR_ROLL_TYPE.get(roll_type, "Standard")
        auto_manual = (product["auto_manual"] or "").lower()
        if "manual" in auto_manual:
            packing_type = "Manual"
            roll_size = "Manual Roll size"
        else:
            packing_type = "Automatic"
            roll_weight = product["roll_weight_kg"] or 0
            roll_size = "Jumbo Roll size" if roll_weight > 25 else "Standard Roll size"

    row = conn.execute(
        """SELECT margin_pct FROM margin_factor
           WHERE film_type=? AND packing_type=? AND roll_size=?
             AND micron_min<=? AND micron_max>=?
           LIMIT 1""",
        (film_type, packing_type, roll_size, micron, micron),
    ).fetchone()
    if row is None:
        return 0.0
    return (row["margin_pct"] or 0.0) / 100.0


# ---------------------------------------------------------------- Extras (v21, Pricing Settings > Extras)
# Three surcharges from the reference app's "Extras" tab, stacked on top of
# the normal EX-Work + margin unit price. See db.EXTRAS_GLOBAL_SETTINGS for
# the seeded defaults/labels (editable in Admin > Global Cost Settings) and
# pricing.py's unit_price_for()/prestretch_unit_price_for() for where each
# one is actually applied.

EXTRAS_SETTING_KEYS = {
    "color": "extra_color_usd_kg",
    "prestretch": "extra_prestretch_usd_kg",
    "foreign_seller_pct": "extra_foreign_seller_pct",
}

def extras_settings(conn):
    """Current value of all three Extras settings, as a plain dict."""
    return {
        "color_usd_kg": _get_setting(conn, EXTRAS_SETTING_KEYS["color"], 0.25),
        "prestretch_usd_kg": _get_setting(conn, EXTRAS_SETTING_KEYS["prestretch"], 0.12),
        "foreign_seller_pct": _get_setting(conn, EXTRAS_SETTING_KEYS["foreign_seller_pct"], 1.0),
    }


def color_extra_usd_kg(conn, colored):
    """'Color extra': an additional $/KG surcharge, applied whenever the
    quotation LINE's own "Colored" checkbox is ticked (v21.1 -- matches the
    reference app exactly: a per-line Colored checkbox, not a property of
    the product/catalog). `colored` is the line's own flag, truthy/falsy."""
    if not colored:
        return 0.0
    return _get_setting(conn, EXTRAS_SETTING_KEYS["color"], 0.25)


def foreign_seller_extra_multiplier(conn, seller_type):
    """'Foreign sellers extra': an extra PERCENTAGE markup (not $/KG) on
    top of the final unit price, for any rep whose account is marked
    'foreign' (user.seller_type) -- stacked on top of that same user's
    existing fixed $/KG Foreign Seller adjustment, not a replacement for
    it. Returns a multiplier (1.0 = no change)."""
    if seller_type != "foreign":
        return 1.0
    pct = _get_setting(conn, EXTRAS_SETTING_KEYS["foreign_seller_pct"], 1.0)
    return 1 + (pct / 100.0)


# ---------------------------------------------------------------- Main EX-Work computation

def compute_ex_work_usd_kg(conn, product, pallet_type=None, rolls_per_pallet_override=None):
    """Full replication of Stretch!AG (EX-Work Cost (KG) - gross weight)
    for the standard product-row case (covers the great majority of SKUs:
    any roll with a Stretch Ability % and a Micron, Automatic or Manual
    packing, standard or jumbo roll weight). See COST_ENGINE.md for the
    pre-stretch-row special case, which this function does not attempt to
    replicate 1:1 (pre-stretch rows in the workbook borrow another row's
    finished sales price rather than being costed independently)."""
    roll_weight = product["roll_weight_kg"] or 0
    core_weight = product["core_weight_kg"] or 0
    plastic_weight = max(roll_weight - core_weight, 0)
    if roll_weight <= 0:
        return 0.0

    waste = _get_setting(conn, "waste_factor", 1.01)
    scrap = _get_setting(conn, "scrap_interest_factor", 1.031)

    comp = material_composition(conn, product)
    c4_fraction = max(1 - sum(comp.values()), 0)

    def mat_cost(key, fraction):
        rate = _material_rate(conn, key)
        return fraction * plastic_weight * rate / 1000 * waste * scrap

    material_cost = mat_cost("c4", c4_fraction)
    material_cost += mat_cost("exceed3518", comp["exceed3518"])
    material_cost += mat_cost("exceed3812", comp["exceed3812"])
    material_cost += mat_cost("exceedxp", comp["exceedxp"])
    material_cost += mat_cost("vista6000", comp["vista6000"])
    material_cost += mat_cost("enable", comp["enable"])
    material_cost += mat_cost("ld", comp["ld"])
    material_cost += mat_cost("vista", comp["vista"])
    # UVI weight fraction is a manual input in the workbook (not BOM-driven);
    # this app does not yet expose a per-product UVI % field, so UVI resin
    # cost is 0 unless/until that's added. See COST_ENGINE.md.

    core_cost = core_cost_usd(conn, product)
    packaging_cost = packaging_cost_per_roll_usd(conn, product, pallet_type, rolls_per_pallet_override)

    interest_rate = _get_setting(conn, "material_interest_rate", 0.0)
    material_interest = material_cost * interest_rate

    roll_type = roll_type_bucket(product["stretch_ability"])
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0
    conv_usd_per_ton = conversion_cost_usd_per_ton(conn, micron, roll_type)
    width_mm = product["width_mm"] if "width_mm" in product.keys() else None
    width = width_mm or 500
    width_factor = (500 / width) if width and width < 500 else 1
    other_costs = plastic_weight * conv_usd_per_ton / 1000 * width_factor

    total = material_cost + core_cost + packaging_cost + material_interest + other_costs
    return round(total / roll_weight, 4)


def breakdown(conn, product):
    """Same as compute_ex_work_usd_kg but returns the itemised components,
    for the admin 'cost preview' screen."""
    roll_weight = product["roll_weight_kg"] or 0
    core_weight = product["core_weight_kg"] or 0
    plastic_weight = max(roll_weight - core_weight, 0)
    waste = _get_setting(conn, "waste_factor", 1.01)
    scrap = _get_setting(conn, "scrap_interest_factor", 1.031)
    comp = material_composition(conn, product)
    c4_fraction = max(1 - sum(comp.values()), 0)

    def mat_cost(key, fraction):
        rate = _material_rate(conn, key)
        return fraction * plastic_weight * rate / 1000 * waste * scrap

    material_cost = mat_cost("c4", c4_fraction)
    for key in ["exceed3518", "exceed3812", "exceedxp", "vista6000", "enable", "ld", "vista"]:
        material_cost += mat_cost(key, comp[key])

    core_cost = core_cost_usd(conn, product)
    packaging_cost = packaging_cost_per_roll_usd(conn, product)
    interest_rate = _get_setting(conn, "material_interest_rate", 0.0)
    material_interest = material_cost * interest_rate
    roll_type = roll_type_bucket(product["stretch_ability"])
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0
    conv_usd_per_ton = conversion_cost_usd_per_ton(conn, micron, roll_type)
    width_mm = product["width_mm"] if "width_mm" in product.keys() else None
    width = width_mm or 500
    width_factor = (500 / width) if width and width < 500 else 1
    other_costs = plastic_weight * conv_usd_per_ton / 1000 * width_factor
    total = material_cost + core_cost + packaging_cost + material_interest + other_costs
    ex_work = round(total / roll_weight, 4) if roll_weight else 0
    return {
        "roll_type": roll_type,
        "bom_multiplier": bom_stretch_multiplier(product["stretch_ability"]),
        "plastic_weight_kg": plastic_weight,
        "material_composition": comp,
        "c4_fraction": c4_fraction,
        "material_cost_usd": round(material_cost, 4),
        "core_cost_usd": round(core_cost, 4),
        "packaging_cost_usd": round(packaging_cost, 4),
        "material_interest_usd": round(material_interest, 4),
        "conversion_cost_usd_per_ton": round(conv_usd_per_ton, 2),
        "other_costs_usd": round(other_costs, 4),
        "total_usd": round(total, 4),
        "ex_work_usd_kg": ex_work,
    }


LABOR_FIXED_WAGES_ITEM = "اجور عمال الانتاج"
LABOR_OVERTIME_ITEM = "اضافي عمال الانتاج"


def labor_totals(conn):
    """(fixed_wage_pool_egp, overtime_pool_egp) for active employees, matching
    'اجور مباشرة' E20 (=SUM of 2024 base wages) and G20 (=SUM of (base/8)*4
    overtime allowance)."""
    rows = conn.execute("SELECT * FROM labor_employee WHERE active=1").fetchall()
    fixed = sum((r["base_2023_egp"] or 0) * (1 + (r["increase_rate"] or 0)) for r in rows)
    overtime = sum(((r["base_2023_egp"] or 0) * (1 + (r["increase_rate"] or 0)) / 8) * 4 for r in rows)
    return fixed, overtime


def sync_labor_to_fixed_costs(conn):
    """Keep the two 'تكاليف ثابتة' fixed-cost line items that are sourced
    from the labor roster ('اجور عمال الانتاج' and, capacity-weighted,
    'اضافي عمال الانتاج') up to date whenever the labor_employee table
    changes. Called after any labor add/edit/delete so the Fixed Costs and
    Cost Preview screens immediately reflect roster changes."""
    fixed_wage_pool, overtime_pool = labor_totals(conn)
    capacity_pct = _get_setting(conn, "capacity_usage_pct", 0.8)
    conn.execute(
        "UPDATE fixed_cost_item SET value_egp=? WHERE name=? AND category='production'",
        (fixed_wage_pool, LABOR_FIXED_WAGES_ITEM),
    )
    conn.execute(
        "UPDATE fixed_cost_item SET value_egp=? WHERE name=? AND category='production'",
        (overtime_pool * capacity_pct, LABOR_OVERTIME_ITEM),
    )
    conn.commit()


def recalculate_all_products(conn):
    """Recompute and store ex_work_usd_kg for every product (the cached
    column). Call after any raw-cost edit that should be reflected in the
    products list / admin display immediately. compute_line()/unit_price_for()
    in pricing.py always recompute live and do not depend on this cache."""
    products = conn.execute("SELECT * FROM product").fetchall()
    for p in products:
        new_val = compute_ex_work_usd_kg(conn, p)
        conn.execute("UPDATE product SET ex_work_usd_kg=? WHERE id=?", (new_val, p["id"]))
    conn.commit()
