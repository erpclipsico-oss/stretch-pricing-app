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

import decimal
import math
import re


def round_half_up(value, decimals=2):
    """Standard "round half up" (a final digit of 5 or more always rounds
    the digit before it up), NOT Python's built-in round(), which uses
    banker's rounding (round-half-to-even) -- round(0.125, 2) is 0.12 in
    plain Python, not 0.13, because 2 is already even. Used everywhere a
    price/weight/total gets its final display rounding, so quoted numbers
    always follow the rounding rule everyone actually learned in school.
    Goes through Decimal (via repr of the float) rather than a float
    epsilon trick, since this is money -- it needs to be exact, not just
    "close enough" for a couple of extra decimal places."""
    if value is None:
        return 0.0
    quant = decimal.Decimal(1).scaleb(-decimals)
    return float(decimal.Decimal(repr(value)).quantize(quant, rounding=decimal.ROUND_HALF_UP))


def round_up(value, decimals=2):
    """v70.2 -- owner-confirmed: FOB/CIF $/KG must match the H1.36
    workbook's own Stretch!AO/AP columns to the cent, and those use Excel's
    ROUNDUP() -- always away from zero, never to nearest -- not ordinary
    rounding (confirmed directly against the workbook's formula text:
    =ROUNDUP(AM10/(AK10*G10*H10),2)). round_half_up() above stays the rule
    for every plain EX-Work/line-total figure (which the sheet's own AI
    column never rounds at all -- its displayed 2dp is just Excel's cell
    format, equivalent to round_half_up for that purpose); this is only for
    the FOB/CIF add-on step, which the sheet deliberately rounds up."""
    if value is None:
        return 0.0
    quant = decimal.Decimal(1).scaleb(-decimals)
    return float(decimal.Decimal(repr(value)).quantize(quant, rounding=decimal.ROUND_CEILING))


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


def material_composition(conn, product, uv_fraction=0.0):
    """Returns dict of material_key -> weight fraction (0..1) for this
    product, pulled from the BOM table using roll-weight threshold (25kg)
    exactly like Stretch!L3:S3 (`IF(H3>25, jumbo column, standard column)`).

    uv_fraction (v36): the quotation LINE's own UV additive %, owner-
    confirmed as a flat 2% for every UV variant (UVI_12m_Power/Power_Plus/
    Standard, UVI_6m_Power/Power_Plus/Standard, UV_Rigid) -- see
    UVI_FRACTION below. Not a property of the product/catalog (matches how
    "Colored" is a per-line flag, not a catalog attribute) -- included here
    as its own "uvi" component so it eats into the C4 leftover fraction the
    same way every other resin component already does
    (c4_fraction = 1 - sum(comp.values()) in compute_ex_work_usd_kg/
    breakdown), rather than being priced on top of a still-100%-full recipe."""
    roll_weight = product["roll_weight_kg"] or 0
    roll_tier = "jumbo" if roll_weight > 25 else "standard"
    mult = bom_stretch_multiplier(product["stretch_ability"])
    micron = float(product["micron"]) if product["micron"] not in (None, "") else 0
    bom = get_bom_row(conn, mult, micron, roll_tier)
    if bom is None:
        comp = {k: 0.0 for k in ["exceed3518", "exceed3812", "exceedxp", "vista6000", "enable", "ld", "vista"]}
    else:
        comp = {
            "exceed3518": bom["exceed3518"] or 0,
            "exceed3812": bom["exceed3812"] or 0,
            "exceedxp": bom["exceedxp"] or 0,
            "vista6000": bom["vista6000"] or 0,
            "enable": bom["enable"] or 0,
            "ld": bom["ld258"] or 0,
            "vista": bom["vista6202"] or 0,
        }
    comp["uvi"] = uv_fraction or 0.0
    return comp


# v36 -- UV additive: owner-confirmed flat 2% weight fraction added to the
# recipe for every UV variant, no matter the duration (6m/12m) or tier
# (Power/Power_Plus/Standard) or UV_Rigid. Its own $/ton rate is the
# existing "uvi" material_rate (already correct at $5500/ton, matching the
# reference app -- was seeded but unused until now). UV_TYPES is the list
# of selectable UV variants, in display order -- each key is also the exact
# margin_factor.film_type string those rows are already seeded under (see
# db.MARGIN_FACTOR_ROWS), so selecting one directly overrides the normal
# roll_type_bucket-based film_type lookup in margin_pct_for() below.
UVI_FRACTION = 0.02
UV_TYPES = [
    ("UVI_12m_Power", "UVI 12m Power"),
    ("UVI_12m_Power_Plus", "UVI 12m Power Plus"),
    ("UVI_12m_Standard", "UVI 12m Standard"),
    ("UVI_6m_Power", "UVI 6m Power"),
    ("UVI_6m_Power_Plus", "UVI 6m Power Plus"),
    ("UVI_6m_Standard", "UVI 6m Standard"),
    ("UV_Rigid", "UV Rigid"),
]

# v39 -- the UV picker in the builder is now a single "UV" checkbox instead
# of a 7-way dropdown, per the owner: it's always the 6-month warranty tier
# (never 12m), and which Power/Power_Plus/Standard/Rigid variant applies is
# derived automatically from the *same* product's own Stretch Ability text
# that already drives its normal (non-UV) margin lookup -- roll_type_bucket()
# -- rather than asked as a separate choice. UV_Rigid stands alone (no 6m/
# 12m split for Rigid in the seeded margin_factor table).
UV_TYPE_FOR_ROLL_TYPE = {
    "St": "UVI_6m_Standard",
    "P": "UVI_6m_Power",
    "P_plus": "UVI_6m_Power_Plus",
    "RIGID": "UV_Rigid",
}


def uv_type_for_product(stretch_ability):
    """The UV_TYPES key to use when the UV checkbox is on for a product
    with this Stretch Ability -- see UV_TYPE_FOR_ROLL_TYPE above."""
    return UV_TYPE_FOR_ROLL_TYPE.get(roll_type_bucket(stretch_ability), "UVI_6m_Standard")


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


# v71 -- owner's own H1.36 sheet has NO Electricity-tab row at all for
# 15-micron (its kW/ton and Tons/day tables jump straight from 12 to 17),
# confirmed directly in the sheet: the "Conversion cost" tab's 15-micron
# row is its own hand-added row that borrows 17-micron's Variable cost
# unchanged (=B21 etc.) but divides 17-micron's Fixed cost by a flat 0.9
# capacity-derate factor (=C21/0.9 etc.) -- for St, P AND P_plus alike.
# The app's own nearest-micron fallback (_lookup_kw_per_ton/_lookup_tons_per_day)
# already reproduces the "borrow 17-micron" half automatically (17 is the
# nearest seeded micron to 15); this derate map reproduces the /0.9 half,
# by shrinking the monthly-tons figure the fixed cost is divided by (a
# smaller monthly_tons raises fixed_cost_per_ton by the same 1/0.9 factor
# ROUNDUP() in the sheet would). Verified against the sheet: reproduces its
# 15-micron Total conversion cost (USD) to the cent for St/P/P+ alike.
CONVERSION_COST_FIXED_DERATE_V71 = {
    15: 0.9,
}


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
    derate = CONVERSION_COST_FIXED_DERATE_V71.get(int(micron)) if float(micron).is_integer() else None
    if derate:
        monthly_tons *= derate

    # v71 -- the sheet's own Electricity!C7:C14 etc. use Excel ROUNDUP()
    # (always rounds UP to the next whole EGP/ton), not nearest -- confirmed
    # the gap directly (17-micron/P+: sheet's kw=1178 x tariff=2.8=3298.4,
    # sheet ROUNDUP->3299, this used to `round()` to 3298, a 1 EGP/ton --
    # ~$0.02/ton -- gap that showed up as a stray 1-cent EX-Work/FOB
    # mismatch on some SKUs in the owner's full-matrix parity check).
    variable_electricity_egp_per_ton = math.ceil(kw_per_ton * variable_tariff - 1e-9)
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


def _pallet_key_for(auto_manual, pallet_size, packaging_group=None, roll_weight_kg=None):
    """auto_manual: 'Automatic' | 'Manual(5kg)' | 'Manual(2.3~3.5kg)' |
    'Manual(2.2kg)' | 'Manual(1.5kg)'. pallet_size: 'Standard' (USD/120x100)
    or 'Euro' (EUR/120x80). packaging_group: a product-level override (see
    product.packaging_group / db.py's _seed_box_packaging_v3) that bypasses
    the normal Automatic/Manual lookup entirely -- e.g. 12-micron 300%
    film, which is boxed (roll -> PE bag -> box) rather than packed the
    standard Automatic way, regardless of its auto_manual value.

    v72 -- owner-confirmed + verified directly against the H1.36 sheet's own
    Stretch!AD formula: the box (PE-bag + carton) packaging ONLY applies to
    12-micron/300%'s STANDARD (<25kg) rolls -- =...'Pallet component'!$Q$22/G
    when H<25. For a JUMBO (>=25kg) roll of that same product the sheet's
    formula switches to the exact same plain 'Pallet component'!$D$11/G every
    other Automatic product uses (the boxed PE-bag/carton pallet_component
    variant is never referenced at all for a jumbo roll of this SKU) -- so
    the packaging_group override is skipped for roll_weight_kg>=25, falling
    through to the normal Automatic/Manual lookup below. Verified against
    the recalculated sheet: this closed a consistent ~$0.017-0.018/kg
    EX-Work gap that showed up ONLY at 50/55/60kg roll weights for 12m/300%,
    never at 16kg."""
    is_eur = "euro" in (pallet_size or "").lower()
    suffix = "eur" if is_eur else "usd"
    if packaging_group and (roll_weight_kg or 0) < 25:
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
    per pallet. Rolls/pallet comes from the Details-sheet packing_tier
    lookup (exact gross-weight bucket x pallet type), not a single
    hardcoded per-product value -- see lookup_packing_tier().

    v21.1 bugfix: this used to subtract the base pallet unit cost for
    jumbo (>25kg) Automatic rolls before dividing by rolls/pallet, on the
    belief that's what the workbook did. Checked directly against the
    H1.36 sheet's own Stretch!AD formula (=...'Pallet component'!$D$11/G39
    for the jumbo/Automatic/USD case) and every jumbo-roll row's own
    "Packaging" column (e.g. row 39, 17mic/300%/50kg/Automatic: 1.008333 =
    the FULL pallet_component total (16.1333) / 16 rolls, no subtraction
    at all) -- there never was a jumbo exclusion; it's a plain
    total/rolls_per_pallet division for every Automatic pallet size,
    jumbo or standard. The exclusion was understating packaging cost by
    exactly one Pallet unit ($12) spread over the pallet's rolls (~$0.75/
    roll for the common 16-roll jumbo pallet) on every jumbo product."""
    rolls_per_pallet = effective_rolls_per_pallet(conn, product, pallet_type, rolls_per_pallet_override)
    if rolls_per_pallet <= 0:
        return 0.0
    packaging_group = product["packaging_group"] if "packaging_group" in product.keys() else None
    key = _pallet_key_for(product["auto_manual"], pallet_type or product["pallet_size"], packaging_group,
                           product["roll_weight_kg"])
    total = pallet_component_total_usd(conn, key)
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


def margin_pct_for(conn, product, pallet_type=None, rolls_per_pallet_override=None, uv_type=None,
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

    uv_type (v36): the quotation LINE's own UV variant selection (one of
    cost_engine.UV_TYPES's keys, e.g. "UVI_12m_Power", or None for a normal
    line). When set, it overrides the normal roll_type_bucket-based
    film_type lookup below with the UV film_type directly -- packing_type/
    roll_size are still derived the usual way (Automatic/Manual x Standard/
    Jumbo/Manual), matching every UVI_*/UV_Rigid row already seeded in
    margin_factor (previously unreachable -- see the old note this replaces:
    "No current catalog product needs a UVI_* or UV_Rigid film_type").

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
        if uv_type:
            film_type = uv_type
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


def apply_hidden_markup(price, markup_mode, markup_value):
    """v44 -- a per-user HIDDEN markup (user.markup_mode/markup_value),
    applied to the FINAL quoted $/KG price for BOTH Stretch Film/Pre-
    Stretch (pricing.py) and PET/PP Strap (strap_pricing.py) lines. Never
    surfaced anywhere in the UI/PDF/Excel breakdown -- same treatment the
    old strap-only user.strap_markup_pct got (which this generalizes and
    replaces; see db.py's migration backfill).

    markup_mode == 'percent': markup_value is percentage points (e.g. 1.5
    for +1.5%), applied multiplicatively -- same rule as the legacy
    strap_markup_pct.
    Anything else (the 'cents_per_kg' mode): markup_value is a flat USD/KG
    amount (e.g. 0.05 for 5 cents/KG) added straight onto the price --
    mirrors how the existing credit-term surcharge is applied in
    strap_pricing.compute_strap_line().
    A user has exactly one mode active at a time (admin's own choice on
    the Users screen), never both."""
    value = markup_value or 0
    if not value:
        return price
    if markup_mode == "percent":
        return price * (1 + value / 100.0)
    return price + value


def capped_discount_pct(conn, line_discount_pct, global_discount_pct):
    """v47 -- owner-requested guardrail: combines a quotation line's own
    Discount % with the quotation's Global Discount % (percentage points,
    added together, same as every call site already did), then silently
    caps the total at the admin-configured 'Max Discount allowed' setting
    (global_setting key db.MAX_DISCOUNT_SETTING_KEY, editable in Admin >
    Global Cost Settings, seeded at 2.0). Applied identically for every
    product family: Stretch Film / Pre-Stretch (where discount_pct comes
    straight off the margin factor -- pricing._discounted_factor()) and
    Strap (where it's a multiplicative discount on the final price --
    strap_pricing.compute_strap_line()) alike, per the owner's explicit
    instruction that this is ONE rule, the same everywhere, not a separate
    cap per product category.

    This is the single place the cap is enforced, and it runs server-side
    on every price calculation AND on save -- so a sales rep typing more
    discount than allowed can never actually make it into a computed or
    saved price, regardless of what the UI does or doesn't catch first.

    Returns (effective_discount_pct, was_capped) -- `was_capped` lets the
    caller warn the rep in the UI that what they typed got reduced."""
    requested = (line_discount_pct or 0) + (global_discount_pct or 0)
    max_allowed = _get_setting(conn, "max_discount_pct", 2.0)
    if max_allowed is not None and max_allowed >= 0 and requested > max_allowed:
        return max_allowed, True
    return requested, False


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


# v70.1 -- owner-confirmed: match the H1.36 workbook's own Stretch!AF3/AF4
# formulas EXACTLY, bugs and all, for 150% Standard at 8 and 9 micron. Those
# two rows' conversion-cost formula reads 'Conversion cost'!J17/J18 (the P+
# / Power Plus column) instead of D17/D18 (the St / Standard column it uses
# for every other 150% Standard micron, e.g. row 4 = 10 micron reads D19) --
# confirmed directly against the workbook's own formula text, not a guess.
# Every other product's conversion-cost lookup is unaffected -- only these
# two (stretch_ability, micron) pairs get the swapped bucket, and only for
# THIS lookup (the margin_factor lookup in margin_pct_for() below stays on
# the normal "St" film_type for these rows -- confirmed the workbook's own
# margin % for these rows matches the plain St-bucket margin table, not a
# Power-Plus one, so only the conversion-cost column reference is swapped
# in the source sheet, nothing else).
CONVERSION_COST_ROLL_TYPE_OVERRIDE_V70 = {
    ("150% Standard", "8"): "P_plus",
    ("150% Standard", "9"): "P_plus",
}


def conversion_roll_type_for(stretch_ability, micron):
    key = (stretch_ability, str(micron))
    if key in CONVERSION_COST_ROLL_TYPE_OVERRIDE_V70:
        return CONVERSION_COST_ROLL_TYPE_OVERRIDE_V70[key]
    return roll_type_bucket(stretch_ability)


# ---------------------------------------------------------------- Main EX-Work computation

def compute_ex_work_usd_kg(conn, product, pallet_type=None, rolls_per_pallet_override=None, uv_fraction=0.0):
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

    comp = material_composition(conn, product, uv_fraction=uv_fraction)
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
    # v36 -- UV additive: owner-confirmed flat 2% weight fraction (see
    # UVI_FRACTION), added into material_composition()'s "uvi" component so
    # it eats into the C4 leftover the same way every other resin does, at
    # the existing "uvi" material_rate ($5500/ton, already seeded/correct).
    material_cost += mat_cost("uvi", comp.get("uvi", 0.0))

    core_cost = core_cost_usd(conn, product)
    packaging_cost = packaging_cost_per_roll_usd(conn, product, pallet_type, rolls_per_pallet_override)

    interest_rate = _get_setting(conn, "material_interest_rate", 0.0)
    material_interest = material_cost * interest_rate

    roll_type = conversion_roll_type_for(product["stretch_ability"], product["micron"])
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
    roll_type = conversion_roll_type_for(product["stretch_ability"], product["micron"])
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
