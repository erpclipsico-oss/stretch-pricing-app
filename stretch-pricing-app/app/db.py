import json
import os
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "pricing.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'sales_rep',
    region TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    price_adjustment_usd_kg REAL NOT NULL DEFAULT 0,
    seller_type TEXT NOT NULL DEFAULT 'local',
    -- v39 -- a hidden per-user markup (percentage points, e.g. 1.5 = 1.5%)
    -- applied on top of PET/PP Strap pricing only, the same way
    -- price_adjustment_usd_kg loads Stretch Film pricing for a given user.
    -- Not shown anywhere in the UI/PDF/Excel breakdown for the sales rep it
    -- applies to -- it just raises their strap quotes silently, admin-only.
    -- Superseded by markup_mode/markup_value below (v44), which does the
    -- same thing but for BOTH Stretch Film and Strap, and supports a flat
    -- cents/KG mode too. Column kept (unused by app code going forward)
    -- so existing DB rows/migrations aren't disturbed.
    strap_markup_pct REAL NOT NULL DEFAULT 0,
    -- v44 -- generalized hidden per-user markup, applied to the FINAL
    -- quoted $/KG price. Superseded by the two independent pairs below
    -- (v46 -- the owner wants Stretch Film and Strap to have their OWN
    -- separate hidden markup, e.g. one user could be +3 cents/KG on
    -- Stretch but +5% on Strap at the same time). Column kept (unused by
    -- app code going forward) so existing DB rows/migrations aren't
    -- disturbed.
    markup_mode TEXT NOT NULL DEFAULT 'percent',
    markup_value REAL NOT NULL DEFAULT 0,
    -- v46 -- independent hidden markup per product family. 'percent'
    -- (percentage points, multiplicative) or 'cents_per_kg' (flat USD/KG,
    -- additive) -- exactly one mode active per user PER FAMILY (a user can
    -- be Percent on Stretch and Cents/KG on Strap at once, or vice versa).
    -- Never shown anywhere in the UI/PDF/Excel. See
    -- cost_engine.apply_hidden_markup().
    stretch_markup_mode TEXT NOT NULL DEFAULT 'percent',
    stretch_markup_value REAL NOT NULL DEFAULT 0,
    strap_markup_mode TEXT NOT NULL DEFAULT 'percent',
    strap_markup_value REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stretch_ability TEXT NOT NULL,
    micron TEXT NOT NULL,
    pallet_size TEXT,
    auto_manual TEXT,
    color TEXT,
    rolls_per_pallet REAL,
    roll_weight_kg REAL,
    core_weight_kg REAL,
    ex_work_usd_kg REAL NOT NULL,
    fob_usd_kg REAL,
    cfr_usd_kg REAL
);

CREATE TABLE IF NOT EXISTS factor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_class TEXT NOT NULL,
    customer_class TEXT NOT NULL,
    roll_size TEXT NOT NULL,
    automatic_standard REAL DEFAULT 0,
    automatic_power REAL DEFAULT 0,
    automatic_power_plus REAL DEFAULT 0,
    uvi_standard REAL DEFAULT 0,
    uvi_power REAL DEFAULT 0,
    uvi_power_plus REAL DEFAULT 0,
    regid REAL DEFAULT 0,
    uv_regid REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS freight (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country TEXT NOT NULL,
    shipping_rate_usd TEXT
);

-- FOB cost differs by which Egyptian port the shipment loads from (port
-- handling/customs/inland-trucking fees) -- a flat $ amount per quotation,
-- editable in admin since rates aren't fixed (v10).
CREATE TABLE IF NOT EXISTS loading_port (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    port TEXT UNIQUE NOT NULL,
    fob_addon_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quotation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_no TEXT UNIQUE,
    customer_name TEXT,
    loading_port TEXT DEFAULT 'Alexandria (Egypt)',
    destination TEXT,
    payment_term TEXT DEFAULT 'Cash (0 days)',
    customer_class TEXT DEFAULT 'A',
    country_class TEXT DEFAULT 'Moderate',
    seller_type TEXT DEFAULT 'Foreign sellers',
    global_discount_pct REAL DEFAULT 0,
    status TEXT DEFAULT 'draft',
    created_by_id INTEGER,
    created_at TEXT,
    FOREIGN KEY (created_by_id) REFERENCES user(id)
);

CREATE TABLE IF NOT EXISTS quotation_line (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_id INTEGER NOT NULL,
    product_id INTEGER,
    pallet_type TEXT DEFAULT 'Standard Pallet',
    packing_type TEXT DEFAULT 'Automatic',
    quantity_pallets REAL DEFAULT 1,
    unit_price_usd_kg REAL DEFAULT 0,
    unit_price_full_usd_kg REAL,
    total_kg REAL DEFAULT 0,
    line_discount_pct REAL DEFAULT 0,
    pricing_basis TEXT NOT NULL DEFAULT 'per_kg',
    -- v30: which product line this row belongs to -- 'stretch_film' (the
    -- original, default) or 'pet'/'pp' (see strap_pricing.py). For a strap
    -- line, product_id points into strap_product, NOT product; the two
    -- id spaces are independent, disambiguated by this column. unit_price
    -- means EX-Work for stretch_film lines but the final CFR (freight
    -- already included) $/kg for strap lines -- deliberately so, because
    -- it lets compute_totals()/view_quotation.html/build_pdf() sum every
    -- line the exact same way with zero changes: each line's unit_price
    -- is simply "this line's contribution to the bottom line", however
    -- that line's own product family prices itself.
    product_line TEXT NOT NULL DEFAULT 'stretch_film',
    -- v32 -- a strap line built with "Custom (width x thickness)" instead
    -- of picking a strap_product catalog row: these carry the full
    -- one-off spec (product_id / strap_product_id is NULL in this case).
    -- Kept on the line itself, the same way Stretch Film's own
    -- custom_roll_weight_kg/custom_core_weight_kg/custom_width_mm already
    -- override a catalog product per quotation.
    strap_custom_bom_key TEXT,
    strap_custom_width_mm REAL,
    strap_custom_thickness_mm REAL,
    strap_custom_meters_per_coil REAL,
    strap_custom_core_weight_kg REAL,
    strap_custom_has_box INTEGER,
    strap_custom_ctr20 INTEGER,
    strap_custom_ctr40 INTEGER,
    -- v42 -- the line's own Pallet checkbox (was only used transiently to
    -- price the line, never saved), so the exported PDF/Excel can show a
    -- real Pallet/Box value for a strap line instead of leaving those
    -- columns blank.
    strap_custom_has_pallet INTEGER,
    -- v36 -- UV additive (Stretch Film lines only): the selected UV variant
    -- key from cost_engine.UV_TYPES (e.g. 'UVI_12m_Power'), or NULL for a
    -- normal line. Not a property of the product/catalog -- a per-line
    -- flag, like "Colored" -- see cost_engine.margin_pct_for()/
    -- compute_ex_work_usd_kg().
    uv_type TEXT,
    FOREIGN KEY (quotation_id) REFERENCES quotation(id)
);

-- v30 -- PET Strap / PP Strap product lines (see strap_pricing.py for the
-- full cost engine). A separate catalog table because the two lines' own
-- dimensions (width/thickness/meters-per-coil, cash/40ft-container flags)
-- don't apply to Stretch Film's roll/pallet-based product table at all.
CREATE TABLE IF NOT EXISTS strap_product (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_key TEXT NOT NULL,        -- 'pet' | 'pp'
    code TEXT NOT NULL,
    bom_key TEXT NOT NULL,
    width_mm REAL NOT NULL,
    thickness_mm REAL NOT NULL,
    meters_per_coil REAL NOT NULL,
    core_weight_kg REAL NOT NULL DEFAULT 1,
    has_box INTEGER NOT NULL DEFAULT 0,
    has_pallet INTEGER NOT NULL DEFAULT 1,
    ctr20 INTEGER NOT NULL DEFAULT 0,
    ctr40 INTEGER NOT NULL DEFAULT 0
);

-- v34 -- PET/PP Strap BOM recipes (composition %, profit %, waste %), now
-- admin-editable instead of frozen as Python constants in strap_pricing.py.
-- components_json holds {component_key: fraction} -- the component keys
-- themselves (which material_rate suffix each maps to) stay structural/code
-- (LINE_CONFIG["components"] in strap_pricing.py), only the numbers here
-- are editable.
CREATE TABLE IF NOT EXISTS strap_bom (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_key TEXT NOT NULL,
    bom_key TEXT NOT NULL,
    profit_pct REAL NOT NULL DEFAULT 0,
    waste_pct REAL NOT NULL DEFAULT 0,
    components_json TEXT NOT NULL,
    UNIQUE(line_key, bom_key)
);

-- v34 -- PET/PP Strap per-line fixed inputs (electricity, fixed cost,
-- direct labor), also now admin-editable instead of frozen constants.
CREATE TABLE IF NOT EXISTS strap_line_config (
    line_key TEXT PRIMARY KEY,
    electricity_per_ton_egp REAL NOT NULL DEFAULT 0,
    fixed_cost_per_kg_usd REAL NOT NULL DEFAULT 0,
    direct_labor_per_kg_usd REAL NOT NULL DEFAULT 0
);

-- ============== Cost engine: raw, editable inputs behind EX-Work cost =====

CREATE TABLE IF NOT EXISTS global_setting (
    key TEXT PRIMARY KEY,
    label TEXT,
    value REAL,
    help TEXT
);

CREATE TABLE IF NOT EXISTS material_rate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_key TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    category TEXT NOT NULL,   -- 'resin' ($/ton, USD) or 'packaging' (EGP/unit)
    unit TEXT,
    value REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS labor_employee (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT,
    base_2023_egp REAL NOT NULL DEFAULT 0,
    increase_rate REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS electricity_power (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    micron REAL NOT NULL,
    roll_type TEXT NOT NULL,   -- St | P | P_plus | RIGID
    kw_per_ton REAL NOT NULL DEFAULT 0,
    UNIQUE(micron, roll_type)
);

CREATE TABLE IF NOT EXISTS production_capacity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    micron REAL NOT NULL,
    roll_type TEXT NOT NULL,
    tons_per_day REAL NOT NULL DEFAULT 0,
    UNIQUE(micron, roll_type)
);

CREATE TABLE IF NOT EXISTS variable_cost_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    value_egp_per_ton REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fixed_cost_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,  -- production | selling | admin | financial
    name TEXT NOT NULL,
    value_egp REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pallet_component (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    packing_key TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    pallet_size_label TEXT,
    pallet_qty REAL NOT NULL DEFAULT 0,
    cardboard_qty REAL NOT NULL DEFAULT 0,
    cap_qty REAL NOT NULL DEFAULT 0,
    corrugated_kg REAL NOT NULL DEFAULT 0,
    stretch_kg REAL NOT NULL DEFAULT 0,
    box_qty REAL NOT NULL DEFAULT 0,
    rolls_per_box REAL NOT NULL DEFAULT 0,
    cartoon_angle_qty REAL NOT NULL DEFAULT 0,
    scotch_tape_qty REAL NOT NULL DEFAULT 0,
    override_note TEXT
);

CREATE TABLE IF NOT EXISTS bom_row (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stretch_multiplier REAL NOT NULL,
    micron REAL NOT NULL,
    roll_tier TEXT NOT NULL,  -- standard | jumbo
    exceed3518 REAL NOT NULL DEFAULT 0,
    exceed3812 REAL NOT NULL DEFAULT 0,
    exceedxp REAL NOT NULL DEFAULT 0,
    vista6000 REAL NOT NULL DEFAULT 0,
    enable REAL NOT NULL DEFAULT 0,
    ld258 REAL NOT NULL DEFAULT 0,
    vista6202 REAL NOT NULL DEFAULT 0,
    UNIQUE(stretch_multiplier, micron, roll_tier)
);

-- Exact gross-weight packing tiers (Details sheet): rolls/box, box/pallet,
-- rolls/pallet and pallets/container by roll-weight bucket x pallet type x
-- Manual/Automatic. Distinct from pallet_component (which prices the
-- packaging *materials* per Automatic/Manual x USD/EUR only) because the
-- workbook's Details sheet breaks rolls-per-pallet and pallets-per-container
-- down further, by the *exact* roll-weight bucket (5kg / 2.2kg / 50kg jumbo
-- / 16kg standard / etc.) -- a distinction pallet_component does not carry.
CREATE TABLE IF NOT EXISTS packing_tier (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tier_key TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,          -- 'Manual' | 'Automatic'
    weight_label TEXT NOT NULL,      -- e.g. 'Manual(5kg)', 'Jumbo Roll (50 kg)'
    pallet_type TEXT NOT NULL,       -- 'Standard' | 'Euro'
    match_weight_kg REAL NOT NULL,   -- representative roll weight used to match a product
    rolls_per_box REAL,
    box_per_pallet REAL,
    rolls_per_pallet REAL NOT NULL DEFAULT 0,
    pallets_per_container40 REAL,
    pallets_per_container20 REAL
);

CREATE TABLE IF NOT EXISTS cost_upload_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    created_by TEXT,
    filename TEXT,
    summary_json TEXT
);

-- Per-table Excel round-trip log (table_sync.py): one row per confirmed
-- single-table upload, so each admin cost screen can show "Last uploaded:
-- <date>" next to its Download/Upload widget. Same known limitation as
-- cost_upload_log and everything else in this app: lost on a Render
-- free-tier restart/redeploy since SQLite has no persistent disk there.
CREATE TABLE IF NOT EXISTS table_upload_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_key TEXT NOT NULL,
    uploaded_at TEXT,
    uploaded_by TEXT,
    filename TEXT,
    summary_json TEXT
);

-- Replaces the old country_class x customer_class x roll_size `factor`
-- table (v18, "Hesham Natora" reference-app margin system -- see the PR
-- description / cost_engine.margin_pct_for() for the full reasoning).
-- Margin is looked up purely by micron x film_type x packing_type x
-- roll_size, independent of any country/customer classification.
-- margin_pct is stored as a raw percentage (17.00 means 17%, matching the
-- reference app's own admin screen and the transcribed seed data below --
-- NOT a 0..1 fraction like the old `factor` table), converted to a
-- fraction at the point of use in cost_engine.margin_pct_for().
CREATE TABLE IF NOT EXISTS margin_factor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    film_type TEXT NOT NULL,       -- 'Power' | 'Power_Plus' | 'Standard' | 'Regular_Rigid' | 'Super_Rigid' |
                                    -- 'UVI_12m_Power' | 'UVI_12m_Power_Plus' | 'UVI_12m_Standard' |
                                    -- 'UVI_6m_Power' | 'UVI_6m_Power_Plus' | 'UVI_6m_Standard' |
                                    -- 'UV_Rigid' | 'Prestretch'
    micron_min REAL NOT NULL,
    micron_max REAL NOT NULL,
    packing_type TEXT NOT NULL,    -- 'Automatic' | 'Manual' | 'Pre-stretch (No Box)' | 'Pre-stretch (Box)'
    roll_size TEXT NOT NULL,       -- 'Standard Roll size' | 'Jumbo Roll size' | 'Manual Roll size' | 'Prestretch Roll size'
    margin_pct REAL NOT NULL DEFAULT 0
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    _seed_reference_data(conn)
    _seed_default_users(conn)
    _seed_cost_engine_data(conn)
    _seed_packing_tiers(conn)
    _seed_prestretch_settings(conn)
    _seed_missing_products(conn)
    _seed_super_rigid_products_v46(conn)
    _seed_freight_v2(conn)
    _seed_packaging_v2(conn)
    _seed_box_packaging_v3(conn)
    _seed_full_import_v4(conn)
    _fix_fixed_cost_duplication_v6(conn)
    _fix_stale_global_settings_v7(conn)
    _seed_margin_factor_v5(conn)
    _seed_extras_settings(conn)
    _fix_stale_material_rates_v8(conn)
    _seed_strap_data(conn)
    _seed_max_discount_setting(conn)
    _fix_uvi_margin_v53(conn)
    _dedupe_stale_jumbo_products_v63(conn)
    _seed_confirmed_micron_gaps_v64(conn)
    conn.close()


def _migrate(conn):
    """Add columns that didn't exist in earlier deployments, so an existing
    live database (already seeded) picks up new fields without a reset."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(user)").fetchall()}
    if "price_adjustment_usd_kg" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN price_adjustment_usd_kg REAL NOT NULL DEFAULT 0")
        conn.commit()
    if "seller_type" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN seller_type TEXT NOT NULL DEFAULT 'local'")
        conn.commit()
        # Backfill: any pre-existing account with a nonzero price adjustment was
        # already being used as a foreign-seller account before this explicit
        # field existed, so mark it 'foreign' rather than leaving it 'local'.
        conn.execute(
            "UPDATE user SET seller_type='foreign' WHERE (price_adjustment_usd_kg IS NOT NULL AND price_adjustment_usd_kg != 0)"
        )
        conn.commit()
    if "strap_markup_pct" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN strap_markup_pct REAL NOT NULL DEFAULT 0")
        conn.commit()
        # Seed the two accounts this was requested for. Runs only once, the
        # moment the column itself is first added -- an admin lowering/
        # clearing it afterwards (e.g. via Admin > Users) is never overwritten
        # on a later boot.
        conn.execute("UPDATE user SET strap_markup_pct=1.5 WHERE username IN ('manuel', 'pasquale')")
        conn.commit()

    if "markup_mode" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN markup_mode TEXT NOT NULL DEFAULT 'percent'")
        conn.commit()
    if "markup_value" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN markup_value REAL NOT NULL DEFAULT 0")
        conn.commit()
        # v44 -- backfill from the legacy strap-only strap_markup_pct so
        # manuel/pasquale (or anyone an admin had already set it for) keep
        # the exact same hidden markup, now applied to Stretch Film too,
        # not just Strap. Runs only once, the moment markup_value itself is
        # first added -- an admin changing it afterwards is never
        # overwritten on a later boot.
        conn.execute(
            "UPDATE user SET markup_mode='percent', markup_value=strap_markup_pct "
            "WHERE strap_markup_pct IS NOT NULL AND strap_markup_pct != 0"
        )
        conn.commit()

    if "stretch_markup_mode" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN stretch_markup_mode TEXT NOT NULL DEFAULT 'percent'")
        conn.commit()
    if "stretch_markup_value" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN stretch_markup_value REAL NOT NULL DEFAULT 0")
        conn.commit()
    if "strap_markup_mode" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN strap_markup_mode TEXT NOT NULL DEFAULT 'percent'")
        conn.commit()
    if "strap_markup_value" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN strap_markup_value REAL NOT NULL DEFAULT 0")
        conn.commit()
        # v46 -- backfill from the old shared markup_mode/markup_value (v44)
        # so anyone already set up (e.g. manuel/pasquale's 1.5%) keeps the
        # exact same markup on BOTH Stretch and Strap at first, which an
        # admin can then split apart independently on the Users screen.
        # Runs only once, the moment strap_markup_value itself is first
        # added -- an admin changing either value afterwards is never
        # overwritten on a later boot.
        conn.execute(
            "UPDATE user SET stretch_markup_mode=markup_mode, stretch_markup_value=markup_value, "
            "strap_markup_mode=markup_mode, strap_markup_value=markup_value "
            "WHERE markup_value IS NOT NULL AND markup_value != 0"
        )
        conn.commit()

    product_cols = {row["name"] for row in conn.execute("PRAGMA table_info(product)").fetchall()}
    if "width_mm" not in product_cols:
        # Stretch!F column ("Width (mm)"). Used by the cost engine's narrow-web
        # surcharge (Conversion cost is spread over a narrower film the same
        # way the workbook does: cost * 500/width when width < 500mm).
        conn.execute("ALTER TABLE product ADD COLUMN width_mm REAL")
        conn.commit()

    line_cols = {row["name"] for row in conn.execute("PRAGMA table_info(quotation_line)").fetchall()}
    if "pricing_basis" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN pricing_basis TEXT NOT NULL DEFAULT 'per_kg'")
        conn.commit()
    if "strap_custom_has_pallet" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN strap_custom_has_pallet INTEGER")
        conn.commit()

    # ---- Pre-Stretch support (v7) ----
    if "is_prestretch" not in product_cols:
        conn.execute("ALTER TABLE product ADD COLUMN is_prestretch INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "prestretch_source_product_id" not in product_cols:
        # The catalog product whose finished, margin-inclusive sales price
        # (Stretch!AI) this Pre-Stretch micron's material cost (Stretch!T)
        # is derived from -- Stretch!T79 = AI49*J79, etc. NULL for every
        # ordinary (non Pre-Stretch) product.
        conn.execute(
            "ALTER TABLE product ADD COLUMN prestretch_source_product_id INTEGER REFERENCES product(id)"
        )
        conn.commit()

    line_cols = {row["name"] for row in conn.execute("PRAGMA table_info(quotation_line)").fetchall()}
    if "prestretch_roll_weight_kg" not in line_cols:
        # Pre-Stretch is made-to-order: roll weight, core weight and
        # rolls/pallet are typed in per quotation line rather than coming
        # from the product catalog (Stretch!H79/I79/G79 are blank in the
        # template for the same reason).
        conn.execute("ALTER TABLE quotation_line ADD COLUMN prestretch_roll_weight_kg REAL")
        conn.commit()
    if "prestretch_core_weight_kg" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN prestretch_core_weight_kg REAL")
        conn.commit()
    if "prestretch_rolls_per_pallet" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN prestretch_rolls_per_pallet REAL")
        conn.commit()
    if "prestretch_packaging_type" not in line_cols:
        # 'no_boxes' | 'boxes' -- Stretch!D79's Auto/Manual-type selector
        # (1 = Packaging for No Boxes, 2 = Packaging for Boxes) reused here
        # as this Pre-Stretch line's packaging choice, since D79 has no
        # other meaning for this product family (Pre-Stretch isn't
        # Automatic/Manual in the usual sense -- it's always hand-rewound).
        conn.execute(
            "ALTER TABLE quotation_line ADD COLUMN prestretch_packaging_type TEXT NOT NULL DEFAULT 'no_boxes'"
        )
        conn.commit()

    # ---- Per-line custom roll spec for EVERY product, not just Pre-Stretch
    # (v9): actual roll weight, core weight and width vary by customer order,
    # so these are left as open/editable fields on each quotation line,
    # pre-filled from the product catalog but overridable; NULL means "use
    # the catalog default" (see cost_engine.with_overrides()).
    line_cols = {row["name"] for row in conn.execute("PRAGMA table_info(quotation_line)").fetchall()}
    if "custom_roll_weight_kg" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN custom_roll_weight_kg REAL")
        conn.commit()
    if "custom_core_weight_kg" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN custom_core_weight_kg REAL")
        conn.commit()
    if "custom_width_mm" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN custom_width_mm REAL")
        conn.commit()
    if "custom_rolls_per_pallet" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN custom_rolls_per_pallet REAL")
        conn.commit()

    # ---- Extras (v21.1): this line's own "Colored" checkbox, independent
    # of the product's catalog color -- drives the Extras "Color extra"
    # $/KG surcharge (see cost_engine.color_extra_usd_kg()).
    if "colored" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN colored INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # ---- Discount-off-margin (v27): the reference ("full", no-discount)
    # unit price is now frozen alongside the actual (discounted) one at
    # save time, so the saved quotation / PDF can show a true Discount $
    # figure without re-deriving the cost-engine formulas later (which
    # could drift if margin_factor or costs change after the quote was
    # saved). NULL on old rows -- treated as "no discount was recorded",
    # i.e. equal to unit_price_usd_kg, everywhere it's read.
    if "unit_price_full_usd_kg" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN unit_price_full_usd_kg REAL")
        conn.commit()

    # ---- Multi product-line (v30): PET Strap / PP Strap alongside Stretch
    # Film. Existing rows are all Stretch Film, hence the default.
    if "product_line" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN product_line TEXT NOT NULL DEFAULT 'stretch_film'")
        conn.commit()
    # strap_product itself is created by SCHEMA's CREATE TABLE IF NOT EXISTS
    # above -- that runs on every boot (conn.executescript(SCHEMA)), fresh or
    # existing DB alike, so no manual ALTER/CREATE is needed for it here.

    # v32 -- "Custom (width x thickness)" strap lines.
    for col, coltype in (
        ("strap_custom_bom_key", "TEXT"),
        ("strap_custom_width_mm", "REAL"),
        ("strap_custom_thickness_mm", "REAL"),
        ("strap_custom_meters_per_coil", "REAL"),
        ("strap_custom_core_weight_kg", "REAL"),
        ("strap_custom_has_box", "INTEGER"),
        ("strap_custom_ctr20", "INTEGER"),
        ("strap_custom_ctr40", "INTEGER"),
    ):
        if col not in line_cols:
            conn.execute(f"ALTER TABLE quotation_line ADD COLUMN {col} {coltype}")
            conn.commit()

    # v36 -- UV additive dropdown (Stretch Film lines only).
    if "uv_type" not in line_cols:
        conn.execute("ALTER TABLE quotation_line ADD COLUMN uv_type TEXT")
        conn.commit()

    # ---- Product-specific packaging override (v15): a handful of products
    # (e.g. 12-micron 300%/350% film) are packed roll-into-PE-bag-into-box
    # rather than the standard Automatic "straight on the pallet, wrapped in
    # stretch film" packaging, per the reference workbook's dedicated
    # "Packaging for Boxes" section for those SKUs. NULL (the default, for
    # every existing product) means "use the normal Automatic/Manual +
    # pallet-type lookup" -- see cost_engine._pallet_key_for(); setting this
    # to a pallet_component packing-key prefix (e.g. 'box_12m300') makes that
    # product always cost against the '<prefix>_usd'/'<prefix>_eur'
    # pallet_component rows instead, regardless of auto_manual.
    product_cols = {row["name"] for row in conn.execute("PRAGMA table_info(product)").fetchall()}
    if "packaging_group" not in product_cols:
        conn.execute("ALTER TABLE product ADD COLUMN packaging_group TEXT")
        conn.commit()

    # Seed the two Egyptian loading ports with their FOB add-on (idempotent,
    # keyed by the unique port name, so it won't duplicate or clobber a rate
    # the owner has since edited in admin).
    conn.execute("INSERT OR IGNORE INTO loading_port (port, fob_addon_usd) VALUES ('Alexandria (Egypt)', 1500)")
    conn.execute("INSERT OR IGNORE INTO loading_port (port, fob_addon_usd) VALUES ('Damietta (Egypt)', 1800)")
    conn.commit()


def _seed_packaging_v2(conn):
    """Add Air bags and PE Bag as packaging material inputs (v14 -- the
    owner pointed out these were missing vs. their reference material list)
    and the matching pallet_component quantity columns so they can be
    costed in. The new material rates are seeded (idempotent, keyed by the
    unique material_key) but the per-pallet QUANTITY columns default to 0 --
    there is no source-of-truth quantity for "air bags per pallet" in this
    app's data yet, so leaving them at 0 keeps every existing price
    unchanged until the owner fills in the real quantity for each packing
    variant in Admin > Pallet/Packaging."""
    conn.execute(
        "INSERT OR IGNORE INTO material_rate (material_key, label, category, unit, value) "
        "VALUES ('air_bag', 'Air bags', 'packaging', 'piece', 200)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO material_rate (material_key, label, category, unit, value) "
        "VALUES ('pe_bag', 'PE Bag', 'packaging', 'kilo', 150)"
    )
    conn.commit()
    pc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(pallet_component)").fetchall()}
    if "air_bag_qty" not in pc_cols:
        conn.execute("ALTER TABLE pallet_component ADD COLUMN air_bag_qty REAL NOT NULL DEFAULT 0")
        conn.commit()
    if "pe_bag_qty" not in pc_cols:
        conn.execute("ALTER TABLE pallet_component ADD COLUMN pe_bag_qty REAL NOT NULL DEFAULT 0")
        conn.commit()


# Box/PE-Bag quantities per pallet variant, straight from the reference
# workbook's 'Pallet component' sheet, "2) Packaging for Boxes" section
# (columns N-V, "12m 300%(16 kg)"). Box qty is a PIECE count (same 'box'
# material rate as the Manual small/large-box variants -- the sheet's
# "kilo" unit label on that row is a copy/paste artifact from the row
# above it; the $ value only reconciles as pieces at the existing box
# rate). 12-micron 300% is a real, sellable catalog product (the owner
# confirmed it -- and confirmed it's the ONLY product with this special
# roll-in-a-PE-bag-then-in-a-box packaging; everything else, including the
# 12-micron 350% product, uses the normal Automatic/Manual packaging).
BOX_PACKAGING_V3 = [
    # packing_key,       label,                                  pallet_size_label,  cardboard, cap, stretch_kg, box_qty, pe_bag_kg
    ("box_12m300_usd", "12m 300% (16kg) - Boxed (USD Pallet)", "120cm x 100cm", 2, 1, 0.5, 46, 2.3),
    ("box_12m300_eur", "12m 300% (16kg) - Boxed (EUR Pallet)", "120cm x 80cm", 4, 0, 0.65, 46, 2.3),
]

# Reference sheet has air bags ONLY on the Manual + EUR-pallet packing
# variants (Large Box 2.3~3.5, Small Box 2.2, Small Box 1.5, Small Box 5 --
# all EUR), at 0.5 air bag per pallet; every USD Manual variant and every
# Automatic variant has none.
AIR_BAG_EUR_PACKING_KEYS = [
    "manual_largebox_eur", "manual_smallbox22_eur", "manual_smallbox15_eur", "manual_smallbox5_eur",
]


def _seed_box_packaging_v3(conn):
    """v16 -- owner confirmed (from the reference workbook they'd already
    uploaded, cross-checked against v15's first guess) that 12-micron 300%
    film is a real, sellable product -- the ONLY one packed roll-in-a-PE-
    bag-then-in-a-box, rather than the standard Automatic straight-on-pallet
    packing every other product (including 12-micron 350%) uses -- and that
    air bags are a Manual+EUR-pallet-only line item. Adds the dedicated
    'Boxed' pallet_component variant (idempotent, keyed by the unique
    packing_key) and, ONE TIME ONLY (gated so it never clobbers a manual
    admin edit on a later restart):
      - sets air_bag_qty=0.5 on the four existing Manual EUR variants
      - adds the 12-micron/300% catalog product itself (it didn't exist
        before) with packaging_group='box_12m300', so its cost actually
        uses the Boxed variant; EX-Work is computed fresh from the cost
        engine right after insert. FOB/CFR are left blank for the owner to
        fill in in Admin > Products -- there's no reference figure for them
        yet.
    """
    for packing_key, label, pallet_size_label, cardboard, cap, stretch_kg, box_qty, pe_bag_kg in BOX_PACKAGING_V3:
        conn.execute(
            """INSERT OR IGNORE INTO pallet_component
               (packing_key, label, pallet_size_label, pallet_qty, cardboard_qty, cap_qty, corrugated_kg,
                stretch_kg, box_qty, rolls_per_box, cartoon_angle_qty, scotch_tape_qty, air_bag_qty, pe_bag_qty)
               VALUES (?, ?, ?, 1, ?, ?, 0, ?, ?, 0, 0, 0, 0, ?)""",
            (packing_key, label, pallet_size_label, cardboard, cap, stretch_kg, box_qty, pe_bag_kg),
        )
    conn.commit()

    already_seeded = conn.execute(
        "SELECT 1 FROM global_setting WHERE key='box_packaging_v3_seeded'"
    ).fetchone()
    if already_seeded:
        return

    for packing_key in AIR_BAG_EUR_PACKING_KEYS:
        conn.execute("UPDATE pallet_component SET air_bag_qty=0.5 WHERE packing_key=?", (packing_key,))

    # 12-micron/350% is NOT special -- make sure it (and anything else) is
    # left on the standard Automatic/Manual packaging lookup.
    conn.execute("UPDATE product SET packaging_group=NULL WHERE packaging_group='box_12m350'")

    existing_300 = conn.execute(
        "SELECT 1 FROM product WHERE micron='12' AND stretch_ability LIKE '%300%'"
    ).fetchone()
    if not existing_300:
        conn.execute(
            """INSERT INTO product
               (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                roll_weight_kg, core_weight_kg, ex_work_usd_kg, fob_usd_kg, cfr_usd_kg, packaging_group)
               VALUES ('300% (Power plus)', '12', 'Standard', 'Automatic', 'Transparent', 46, 16, 1.8,
                       0, NULL, NULL, 'box_12m300')"""
        )
        conn.commit()

    conn.execute(
        "INSERT INTO global_setting (key, label, value, help) VALUES (?, ?, ?, ?)",
        ("box_packaging_v3_seeded", "Box packaging v3 seeded (internal marker)", 1,
         "Internal marker: the 12m 300% Boxed packaging variant, the 12-micron/300% catalog "
         "product, and the Manual+EUR air-bag defaults have been loaded. Do not delete this "
         "row -- it stops the one-time refresh from running again and overwriting manual "
         "edits made in Admin > Pallet/Packaging, Admin > Products, or a product's packaging "
         "group."),
    )
    conn.commit()
    from . import cost_engine
    cost_engine.recalculate_all_products(conn)


def _seed_freight_v2(conn):
    """Replace the placeholder freight rows with the owner's real Alexandria
    sea-freight rate sheet (USD/container, per destination). Gated by a
    one-time marker in global_setting, and called LAST in init_db() (after
    _seed_reference_data(), which on a brand-new database -- e.g. every
    restart on Render's free tier with no persistent disk -- inserts its own
    placeholder freight rows from seed_data.json) so this always has the
    final say and never leaves stale placeholder rows mixed in. Never
    overwrites a rate an admin edits afterward through Admin > Freight."""
    already_seeded = conn.execute(
        "SELECT 1 FROM global_setting WHERE key='freight_v2_seeded'"
    ).fetchone()
    if already_seeded:
        return
    conn.execute("DELETE FROM freight")
    for country, rate in FREIGHT_RATES_V2:
        conn.execute("INSERT INTO freight (country, shipping_rate_usd) VALUES (?, ?)", (country, str(rate)))
    conn.execute(
        "INSERT INTO global_setting (key, label, value, help) VALUES (?, ?, ?, ?)",
        ("freight_v2_seeded", "Freight v2 seeded (internal marker)", 1,
         "Internal marker: the real Alexandria freight rate sheet has been loaded. "
         "Do not delete this row -- it stops the one-time freight refresh from "
         "running again and wiping out manual edits made in Admin > Freight."),
    )
    conn.commit()


def _seed_reference_data(conn):
    existing = conn.execute("SELECT COUNT(*) c FROM product").fetchone()["c"]
    if existing:
        return
    with open(os.path.join(BASE_DIR, "data", "seed_data.json")) as f:
        data = json.load(f)

    for p in data["products"]:
        conn.execute(
            """INSERT INTO product
               (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                roll_weight_kg, core_weight_kg, ex_work_usd_kg, fob_usd_kg, cfr_usd_kg)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (p["stretch_ability"], str(p["micron"]), p.get("pallet_size"), p.get("auto_manual"),
             p.get("color"), p.get("rolls_per_pallet"), p.get("roll_weight_kg"), p.get("core_weight_kg"),
             p["ex_work_usd_kg"], p.get("fob_usd_kg"), p.get("cfr_usd_kg")),
        )

    for f in data["factors"]:
        conn.execute(
            """INSERT INTO factor
               (country_class, customer_class, roll_size, automatic_standard, automatic_power,
                automatic_power_plus, uvi_standard, uvi_power, uvi_power_plus, regid, uv_regid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (f["country_class"], f["customer_class"], f["roll_size"],
             f.get("automatic_standard") or 0, f.get("automatic_power") or 0, f.get("automatic_power_plus") or 0,
             f.get("uvi_standard") or 0, f.get("uvi_power") or 0, f.get("uvi_power_plus") or 0,
             f.get("regid") or 0, f.get("uv_regid") or 0),
        )

    for fr in data["freight"]:
        conn.execute("INSERT INTO freight (country, shipping_rate_usd) VALUES (?, ?)",
                     (fr["country"], str(fr["shipping_rate_usd"])))

    conn.commit()


# ---------------------------------------------------------------------
# v7: the seven "jumbo pre-stretch precursor" SKUs (Stretch rows 27, 29,
# 31, 35, 40, 49 -- row 32 is a verbatim duplicate of row 31, '23J-pre'
# under 250% Power, and is intentionally NOT added as a second row; see
# COST_ENGINE.md) plus the "Pre-Stretch" product line (Stretch rows
# 79-85). Both are seeded idempotently, matching on natural keys, so this
# is safe to re-run against an already-deployed, already-seeded live DB.
# ---------------------------------------------------------------------

# (stretch_ability, micron, roll_weight_kg, rolls_per_pallet, core_weight_kg, width_mm)
# -- all Auto/Manual=Automatic, Pallet size=Standard, Color=1 (Transparent),
# per Stretch!C/D/E columns for these rows (verified directly against the
# workbook, not assumed).
JUMBO_PRESTRETCH_PRECURSOR_PRODUCTS = [
    ("250% Power", "17", 50, 16, 1.8, 500),   # Stretch row 27 ('17J-pre')
    ("250% Power", "20", 50, 16, 1.8, 500),   # Stretch row 29 ('20J-pre')
    ("250% Power", "23", 50, 16, 1.8, 500),   # Stretch row 31 ('23J-pre'); row 32 is a duplicate, skipped
    ("250% Power", "30", 50, 16, 1.8, 500),   # Stretch row 35 ('30J-pre')
    ("300% (Power plus)", "17", 50, 16, 1.8, 500),  # Stretch row 40 ('17J-pre')
    ("350% (Power plus)", "17", 50, 16, 1.8, 500),  # Stretch row 49 ('17J-pre')
]

# Pre-Stretch micron -> which of the jumbo precursor SKUs above (identified
# by stretch_ability+micron, roll_weight_kg=50) supplies its material cost
# (Stretch!T79 = AI<source row>*J79, i.e. the source SKU's finished,
# margin-inclusive sales $/KG x this line's entered net weight).
# Pre-Stretch micron 10 (Stretch row 84, source row 32) intentionally
# shares its source with micron 9 (row 83, source row 31) since row 32 is
# the duplicate of row 31 noted above -- both point at the same
# '250% Power' / 23 micron jumbo SKU.
PRESTRETCH_PRODUCTS = [
    # (micron, source_stretch_ability, source_micron)
    ("5", "350% (Power plus)", "17"),   # Stretch row 79, source = row 49
    ("6", "300% (Power plus)", "17"),   # Stretch row 80, source = row 40
    ("7", "250% Power", "17"),          # Stretch row 81, source = row 27
    ("8", "250% Power", "20"),          # Stretch row 82, source = row 29
    ("9", "250% Power", "23"),          # Stretch row 83, source = row 31
    ("10", "250% Power", "23"),         # Stretch row 84, source = row 32 (dup of row 31)
    ("12", "250% Power", "30"),         # Stretch row 85, source = row 35
]

PRESTRETCH_GLOBAL_SETTINGS = [
    # key, label, value, help
    ("prestretch_packaging_noboxes_usd", "Pre-Stretch packaging total, No Boxes ($/pallet)", 14.8,
     "'Pallet component'!Q11. Used as Stretch!AD79's numerator when the line's packaging type is "
     "'No Boxes' (D79=1): divided by the line's entered Rolls/Pallet."),
    ("prestretch_packaging_boxes_usd", "Pre-Stretch packaging total, With Boxes ($/pallet)", 14.38,
     "'Pallet component'!V11. Used as Stretch!AD79's numerator when the line's packaging type is "
     "'With Boxes' (D79=2): divided by Rolls/Pallet, plus 1/6 of a Box's cost "
     "('Material pricing'!C21/F2/6, i.e. the 'box' material rate)."),
]


# v21: "Pricing Settings > Extras" (Hesham Natora reference app) -- three
# surcharges applied on top of the normal margin-priced unit price, per the
# owner's explicit instruction to add this section AND apply it (not just
# store the numbers). See cost_engine.color_extra_usd_kg()/extras_settings()
# and pricing.py's unit_price_for()/prestretch_unit_price_for() for where
# each one is actually used.
EXTRAS_GLOBAL_SETTINGS = [
    # key, label, value, help
    ("extra_color_usd_kg", "Extras - Color extra ($/KG)", 0.25,
     "Added to the unit price of any line whose product color is not "
     "Transparent/Clear/Natural (Pricing Settings > Extras > 'Color extra')."),
    ("extra_prestretch_usd_kg", "Extras - Prestretch extra ($/KG)", 0.12,
     "Added to the unit price of every Pre-Stretch line, on top of its "
     "normal EX-Work + margin ('Extras > Prestretch extra')."),
    ("extra_foreign_seller_pct", "Extras - Foreign sellers extra (% of selling price)", 1.0,
     "Applied as an extra percentage markup (not $/KG) on top of the final "
     "unit price -- stacked on top of the existing fixed Foreign Seller "
     "$/KG adjustment on the Users page -- for any rep whose account is "
     "marked 'Foreign' ('Extras > Foreign sellers extra', mode = Percent of "
     "selling price). Enter as a plain percentage, e.g. 1 = 1%."),
]


def _seed_extras_settings(conn):
    """Per-key idempotent (like _seed_prestretch_settings) so these three
    settings get added to an already-deployed live DB too, without ever
    overwriting a value the owner has since edited in Admin."""
    for key, label, value, help_text in EXTRAS_GLOBAL_SETTINGS:
        exists = conn.execute("SELECT key FROM global_setting WHERE key=?", (key,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO global_setting (key, label, value, help) VALUES (?,?,?,?)",
            (key, label, value, help_text),
        )
    conn.commit()


# v47 -- owner-requested guardrail: a sales rep's Discount % (this line's
# own + the quotation's Global Discount %, combined) must never be allowed
# to eat more of the profit margin than the owner has approved -- e.g. a
# Standard Film line with an 8% margin should never drop below 6% margin
# just because a rep mistyped the discount. One single value, applied the
# SAME way to every product family (Stretch Film, Pre-Stretch and Strap
# alike) -- see cost_engine.capped_discount_pct(), the one place this
# setting is read and enforced (server-side, so it's never possible to
# compute a line "wrong" no matter what a rep types in the UI).
MAX_DISCOUNT_SETTING_KEY = "max_discount_pct"


def _seed_max_discount_setting(conn):
    """Per-key idempotent, same pattern as _seed_extras_settings -- adds
    this to an already-deployed live DB too, without ever overwriting a
    value the owner has since edited in Admin > Global Cost Settings."""
    exists = conn.execute(
        "SELECT key FROM global_setting WHERE key=?", (MAX_DISCOUNT_SETTING_KEY,)
    ).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO global_setting (key, label, value, help) VALUES (?,?,?,?)",
        (MAX_DISCOUNT_SETTING_KEY, "Max Discount allowed (% points off margin)", 2.0,
         "The highest combined Discount % (this quotation line's own Discount % plus "
         "the quotation's Global Discount %, added together) allowed on ANY line -- "
         "Stretch Film, Pre-Stretch or Strap alike. If a sales rep enters more than "
         "this, the system silently caps it at this value so the quoted price is "
         "never computed with more discount than the owner approved. Set higher to "
         "allow bigger discounts (e.g. 100 effectively removes the cap)."),
    )
    conn.commit()


def _seed_missing_products(conn):
    """Idempotent, per-row seeding (not gated on the product table being
    empty) so these SKUs get added to an already-deployed, already-seeded
    live database too, without duplicating rows if this runs again."""
    from . import cost_engine

    def find_jumbo_id(stretch_ability, micron):
        row = conn.execute(
            "SELECT id FROM product WHERE stretch_ability=? AND micron=? AND roll_weight_kg=50",
            (stretch_ability, micron),
        ).fetchone()
        return row["id"] if row else None

    for stretch_ability, micron, roll_weight_kg, rolls_per_pallet, core_weight_kg, width_mm in \
            JUMBO_PRESTRETCH_PRECURSOR_PRODUCTS:
        exists = conn.execute(
            "SELECT id FROM product WHERE stretch_ability=? AND micron=? AND roll_weight_kg=?",
            (stretch_ability, micron, roll_weight_kg),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO product
               (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                roll_weight_kg, core_weight_kg, width_mm, ex_work_usd_kg)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (stretch_ability, micron, "Standard", "Automatic", "Transparent",
             rolls_per_pallet, roll_weight_kg, core_weight_kg, width_mm),
        )
    conn.commit()

    # Now that all jumbo precursor SKUs exist (whether just-inserted or
    # already present from an earlier run), resolve each Pre-Stretch
    # micron's source product id and seed the Pre-Stretch catalog rows.
    for micron, source_ability, source_micron in PRESTRETCH_PRODUCTS:
        exists = conn.execute(
            "SELECT id FROM product WHERE stretch_ability='Pre-Stretch' AND micron=?", (micron,)
        ).fetchone()
        if exists:
            continue
        source_id = find_jumbo_id(source_ability, source_micron)
        conn.execute(
            """INSERT INTO product
               (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                roll_weight_kg, core_weight_kg, width_mm, ex_work_usd_kg, is_prestretch,
                prestretch_source_product_id)
               VALUES ('Pre-Stretch', ?, NULL, 'Manual', 'Transparent', NULL, NULL, NULL, NULL, 0, 1, ?)""",
            (micron, source_id),
        )
    conn.commit()

    # Cache ex_work_usd_kg for the newly-seeded jumbo SKUs (ordinary
    # products -- the cost engine prices them exactly like any other
    # jumbo roll). Pre-Stretch rows are left at 0: they have no fixed
    # roll/core weight to compute a cached rate from (made-to-order), so
    # their price is always computed live, per quotation line, from the
    # entered weights -- see pricing.compute_prestretch_line().
    for stretch_ability, micron, roll_weight_kg, *_ in JUMBO_PRESTRETCH_PRECURSOR_PRODUCTS:
        row = conn.execute(
            "SELECT * FROM product WHERE stretch_ability=? AND micron=? AND roll_weight_kg=?",
            (stretch_ability, micron, roll_weight_kg),
        ).fetchone()
        if row:
            new_val = cost_engine.compute_ex_work_usd_kg(conn, row)
            conn.execute("UPDATE product SET ex_work_usd_kg=? WHERE id=?", (new_val, row["id"]))
    conn.commit()



# v46 -- "Super" grade Rigid film. The owner reported the Rigid catalog only
# had 2 microns (10/12) -- those 2 are the "Regular" grade (stretch_ability
# 'REGID Film'), reverse-engineered from the H1.24 sheet's own Stretch!63-69
# rows (width 500mm, 46 rolls/pallet, 16kg roll, 1.8kg core -- exactly what's
# already seeded). Re-checking every reference workbook against the actual
# request found a SEPARATE, entirely un-seeded "Super" grade in the newer
# H1.36 sheet's Stretch!63-76 block (distinct stretch_ability text, "Super
# REGID Film"): only 3 of its rows have real roll-spec numbers filled in
# (10/12/15 micron -- every other micron in that block, and the "Regular"
# sub-rows alongside them, are blank template rows with no width/rolls/
# weight/core at all, so they're deliberately NOT added here, same
# judgement call as the original H1.36 import skipped them for).
#
# That sheet's own "Auto /Manual" column uses non-boolean codes (3, 4) with
# no legend in the workbook -- but each row's own Rolls/Pallet figure
# (360 / 480 / 480) is an EXACT match to this app's own Manual packing_tier
# table (Manual(2.3~3.5kg)=360, Manual(2.2kg)=480), and the roll weights
# (2.8/1.8/2.17kg) sit squarely in the Manual weight range, nowhere near the
# Automatic tiers (16/50/55kg) -- so these are seeded as Manual, resolved
# from the tier match rather than guessed from the ambiguous numeric code.
# Regular_Rigid/Super_Rigid margin_factor rows are numerically identical
# (see cost_engine.FILM_TYPE_FOR_ROLL_TYPE's comment), so no new margin data
# is needed -- roll_type_bucket() already buckets any '...regid...' text
# onto the same Rigid margin lookup regardless of Super vs Regular.
SUPER_RIGID_PRODUCTS = [
    # micron, rolls_per_pallet, roll_weight_kg, core_weight_kg, width_mm
    ("10", 360, 2.8, 0.3, 450),
    ("12", 480, 1.8, 0.3, 450),
    ("15", 480, 2.17, 0.3, 450),
]


def _seed_super_rigid_products_v46(conn):
    """Idempotent per-row (like _seed_missing_products) so this adds cleanly
    to an already-deployed, already-seeded live DB too."""
    from . import cost_engine

    for micron, rolls_per_pallet, roll_weight_kg, core_weight_kg, width_mm in SUPER_RIGID_PRODUCTS:
        exists = conn.execute(
            "SELECT id FROM product WHERE stretch_ability='Super REGID Film' AND micron=?", (micron,)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO product
               (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                roll_weight_kg, core_weight_kg, width_mm, ex_work_usd_kg)
               VALUES ('Super REGID Film', ?, 'Standard', 'Manual', 'Transparent', ?, ?, ?, ?, 0)""",
            (micron, rolls_per_pallet, roll_weight_kg, core_weight_kg, width_mm),
        )
    conn.commit()

    for micron, *_ in SUPER_RIGID_PRODUCTS:
        row = conn.execute(
            "SELECT * FROM product WHERE stretch_ability='Super REGID Film' AND micron=?", (micron,)
        ).fetchone()
        if row:
            new_val = cost_engine.compute_ex_work_usd_kg(conn, row)
            conn.execute("UPDATE product SET ex_work_usd_kg=? WHERE id=?", (new_val, row["id"]))
    conn.commit()


def _seed_prestretch_settings(conn):
    """Per-key idempotent (like _seed_default_users) so these two fixed
    packaging totals get added to an already-deployed live DB too."""
    for key, label, value, help_text in PRESTRETCH_GLOBAL_SETTINGS:
        exists = conn.execute("SELECT key FROM global_setting WHERE key=?", (key,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO global_setting (key, label, value, help) VALUES (?,?,?,?)",
            (key, label, value, help_text),
        )
    conn.commit()


def _seed_cost_engine_data(conn):
    """Seed the raw-cost tables from the workbook's current values, once.
    Each table is seeded independently (guarded by its own emptiness check)
    so a live DB that already has some of these tables populated from an
    earlier deploy is never wiped or duplicated."""
    with open(os.path.join(BASE_DIR, "data", "cost_seed.json")) as f:
        data = json.load(f)

    if not conn.execute("SELECT COUNT(*) c FROM global_setting").fetchone()["c"]:
        for s in data["global_settings"]:
            conn.execute(
                "INSERT INTO global_setting (key, label, value, help) VALUES (?,?,?,?)",
                (s["key"], s["label"], s["value"], s.get("help")),
            )

    if not conn.execute("SELECT COUNT(*) c FROM material_rate").fetchone()["c"]:
        for m in data["material_rates"]:
            conn.execute(
                "INSERT INTO material_rate (material_key, label, category, unit, value) VALUES (?,?,?,?,?)",
                (m["material_key"], m["label"], m["category"], m.get("unit"), m["value"]),
            )

    if not conn.execute("SELECT COUNT(*) c FROM labor_employee").fetchone()["c"]:
        for e in data["labor_employees"]:
            conn.execute(
                "INSERT INTO labor_employee (name, role, base_2023_egp, increase_rate) VALUES (?,?,?,?)",
                (e["name"], e.get("role"), e["base_2023_egp"], e["increase_rate"]),
            )

    if not conn.execute("SELECT COUNT(*) c FROM electricity_power").fetchone()["c"]:
        for e in data["electricity_power"]:
            conn.execute(
                "INSERT OR IGNORE INTO electricity_power (micron, roll_type, kw_per_ton) VALUES (?,?,?)",
                (e["micron"], e["roll_type"], e["kw_per_ton"]),
            )

    if not conn.execute("SELECT COUNT(*) c FROM production_capacity").fetchone()["c"]:
        for e in data["production_capacity"]:
            conn.execute(
                "INSERT OR IGNORE INTO production_capacity (micron, roll_type, tons_per_day) VALUES (?,?,?)",
                (e["micron"], e["roll_type"], e["tons_per_day"]),
            )

    if not conn.execute("SELECT COUNT(*) c FROM variable_cost_item").fetchone()["c"]:
        for v in data["variable_cost_items"]:
            conn.execute(
                "INSERT INTO variable_cost_item (name, value_egp_per_ton) VALUES (?,?)",
                (v["name"], v["value_egp_per_ton"]),
            )

    if not conn.execute("SELECT COUNT(*) c FROM fixed_cost_item").fetchone()["c"]:
        for fx in data["fixed_cost_items"]:
            conn.execute(
                "INSERT INTO fixed_cost_item (category, name, value_egp) VALUES (?,?,?)",
                (fx["category"], fx["name"], fx["value_egp"]),
            )

    if not conn.execute("SELECT COUNT(*) c FROM pallet_component").fetchone()["c"]:
        for p in data["pallet_components"]:
            conn.execute(
                """INSERT INTO pallet_component
                   (packing_key, label, pallet_size_label, pallet_qty, cardboard_qty, cap_qty,
                    corrugated_kg, stretch_kg, box_qty, rolls_per_box, cartoon_angle_qty,
                    scotch_tape_qty, override_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p["packing_key"], p["label"], p.get("pallet_size_label"), p.get("pallet_qty") or 0,
                 p.get("cardboard_qty") or 0, p.get("cap_qty") or 0, p.get("corrugated_kg") or 0,
                 p.get("stretch_kg") or 0, p.get("box_qty") or 0, p.get("rolls_per_box") or 0,
                 p.get("cartoon_angle_qty") or 0, p.get("scotch_tape_qty") or 0, p.get("override_note")),
            )

    if not conn.execute("SELECT COUNT(*) c FROM bom_row").fetchone()["c"]:
        for b in data["bom_rows"]:
            conn.execute(
                """INSERT OR IGNORE INTO bom_row
                   (stretch_multiplier, micron, roll_tier, exceed3518, exceed3812, exceedxp,
                    vista6000, enable, ld258, vista6202)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (b["stretch_multiplier"], b["micron"], b["roll_tier"], b["exceed3518"], b["exceed3812"],
                 b["exceedxp"], b["vista6000"], b["enable"], b["ld258"], b["vista6202"]),
            )

    conn.commit()

    # Keep the two labor-sourced fixed-cost line items consistent with the
    # labor_employee roster right after the initial seed.
    from . import cost_engine
    cost_engine.sync_labor_to_fixed_costs(conn)


# Real Alexandria sea-freight rate sheet (USD/container), supplied by the
# owner (v13) to replace the placeholder freight rows seeded earlier.
FREIGHT_RATES_V2 = [
    ("Belgium - Antwerp", 1600),
    ("Bulgaria - Burgas", 1350),
    ("Bulgaria - Varna", 1350),
    ("Cyprus - Limassol", 1000),
    ("Czech Republic - DAP", 4000),
    ("DAP France - Astic emballage - Rubafilm", 3000),
    ("Felixstowe - The United Kingdom", 1600),
    ("Germany - Hamburg", 1200),
    ("Greece - DAP Hellagro", 3000),
    ("Greece - Pireaus", 1000),
    ("Greece - Thessaloniki", 1000),
    ("Italy - Ancona", 1100),
    ("Italy - Genoa", 1100),
    ("Italy - La Spezia", 1100),
    ("Italy - Napoli", 1100),
    ("Italy - Salerno", 1100),
    ("Italy - Venice", 1100),
    ("lebanon - Beirut", 900),
    ("Lithuania - klaipeda", 1100),
    ("Morroco - Casablanca", 2000),
    ("Netherlands - Rotterdam", 1350),
    ("Newton Company - UK", 3600),
    ("Poland - DAP Bialpack", 3500),
    ("Poland - Gdańsk", 1550),
    ("Portugal - Leixões", 1100),
    ("Portugal - Lisbon", 1100),
    ("Romania - Constanta", 1450),
    ("Romania - Dap Ambafol", 3500),
    ("Romania - DAP - Aplan", 3500),
    ("Saudi Arabia - Jeddah", 850),
    ("Slovenia - Koper", 1000),
    ("Spain - Barcelona", 1000),
    ("Spain - Valencia", 1000),
    ("Turkey - Ambarli", 650),
    ("Turkiye - Istanbul", 450),
    ("Turkiye - Izmir", 400),
    ("UK - Felixstowe", 1100),
]

DEFAULT_USERS = [
    # username, full_name, role, region, seller_type, price_adjustment_usd_kg
    ("admin", "Administrator", "admin", "Egypt", "local", 0),
    ("sales_rep1", "Sales Rep", "sales_rep", "Egypt", "local", 0),
    ("pasquale", "Pasquale", "sales_rep", "Foreign", "foreign", 0.03),
    ("manuel", "Manuel", "sales_rep", "Foreign", "foreign", 0.03),
]


PACKING_TIERS = [
    # tier_key, category, weight_label, pallet_type, match_weight_kg,
    # rolls_per_box, box_per_pallet, rolls_per_pallet, pallets/container40, pallets/container20
    ("manual_5kg_standard", "Manual", "Manual(5kg)", "Standard", 5, 4, 64, 256, 20, 10),
    ("manual_2.3_3.5kg_standard", "Manual", "Manual(2.3~3.5kg)", "Standard", 2.9, 6, 60, 360, None, None),
    ("manual_2.2kg_standard", "Manual", "Manual(2.2kg)", "Standard", 2.2, 6, 80, 480, None, None),
    ("manual_1.5kg_standard", "Manual", "Manual(1.5kg)", "Standard", 1.5, 6, 112, 672, None, None),
    ("manual_5kg_euro", "Manual", "Manual(5kg)", "Euro", 5, 4, 48, 192, 24, 10),
    ("manual_2.3_3.5kg_euro", "Manual", "Manual(2.3~3.5kg)", "Euro", 2.9, 6, 36, 216, None, None),
    ("manual_2.2kg_euro", "Manual", "Manual(2.2kg)", "Euro", 2.2, 6, 64, 384, None, None),
    ("manual_1.5kg_euro", "Manual", "Manual(1.5kg)", "Euro", 1.5, 6, 84, 504, None, None),
    ("jumbo_50kg_standard", "Automatic", "Jumbo Roll (50 kg)", "Standard", 50, None, None, 16, 31, 20),
    ("jumbo_55kg_standard", "Automatic", "Jumbo Roll (55 kg)", "Standard", 55, None, None, 16, 28, None),
    ("standard_16kg_standard", "Automatic", "Standard Roll (16 kg)", "Standard", 16, None, None, 46, 34, None),
    ("jumbo_50kg_euro", "Automatic", "Jumbo Roll (50 kg)", "Euro", 50, None, None, 16, None, None),
    ("jumbo_55kg_euro", "Automatic", "Jumbo Roll (55 kg)", "Euro", 55, None, None, 16, None, None),
    # Details sheet lists two Euro/Standard-Roll(16kg) rows (45 and 30 rolls/pallet
    # variants) without labelling which applies when; the first (45 rolls/pallet)
    # is kept as the canonical default here -- flagged in COST_ENGINE.md as an
    # open question for the team to confirm.
    ("standard_16kg_euro", "Automatic", "Standard Roll (16 kg)", "Euro", 16, None, None, 45, 24, 11),
]


def _seed_packing_tiers(conn):
    if conn.execute("SELECT COUNT(*) c FROM packing_tier").fetchone()["c"]:
        return
    for (tier_key, category, weight_label, pallet_type, match_weight_kg, rolls_per_box,
         box_per_pallet, rolls_per_pallet, p40, p20) in PACKING_TIERS:
        conn.execute(
            """INSERT OR IGNORE INTO packing_tier
               (tier_key, category, weight_label, pallet_type, match_weight_kg, rolls_per_box,
                box_per_pallet, rolls_per_pallet, pallets_per_container40, pallets_per_container20)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tier_key, category, weight_label, pallet_type, match_weight_kg, rolls_per_box,
             box_per_pallet, rolls_per_pallet, p40, p20),
        )
    conn.commit()


def _seed_full_import_v4(conn):
    """v17 -- owner-supplied reference workbook 'Stretch_Export_Pricing_H1.36':
    full one-time refresh of material rates, BOM, products (Stretch sheet),
    electricity/production-capacity, fixed costs, labor roster, factors and
    pallet-component quantities from that workbook, per the owner's explicit
    "enter ALL the products and BOMs, update material prices, take ALL the
    data in it and apply it" instruction.

    Gated behind a global_setting marker (like every other one-time refresh
    in this file) so a later gunicorn worker / Render restart never re-runs
    this and clobbers an admin's manual edit made earlier in the same boot.
    Called LAST in init_db() so it always has the final say over whatever
    the earlier seed steps loaded.

    Deliberately NOT touched by this import (see PR/report for the reasoning
    behind each):
      - 'pe_bag' material rate: stays at the owner's explicit 150 EGP/kg
        instruction from an earlier conversation turn, not the ~95 EGP/kg
        this workbook's Pallet component sheet implies.
      - bom_row's C4 column: the workbook's explicit 'C4' fraction always
        equals 1 minus the sum of the other resins on that row (verified
        against every sampled row), which is exactly what
        cost_engine.material_composition()'s c4_fraction leftover
        computation already reproduces -- so no schema change or extra
        column is needed; importing the other 7 fractions is sufficient.
      - factor.regid / factor.uv_regid: the new Factors sheet splits Rigid
        factors by micron bucket ("8&9" vs "10&12" automatic, plus separate
        "Rigid"/"UV&Rigid" columns) in a way the existing single-value
        regid/uv_regid columns can't represent without guessing which
        bucket an admin meant -- left as-is rather than risk a wrong value.
      - 'Labor Jan 2026' sheet: NOT used as the labor source. It's a
        company-wide monthly payroll ledger (security, cleaning, warehouse,
        admin -- not just production) with no base/increase-rate shape, so
        it doesn't map onto labor_employee's schema or scope. 'Direct
        Labor' (this app's existing table's shape and role-for-role match:
        'رئيس وردية' / 'عامل انتاج' production floor staff) is used instead.
      - REGID Film 'Super"10/12/15"' rows on the Stretch sheet: these use a
        different Auto/Manual + Pallet-size numeric coding than the rest of
        the sheet with no legend found in the workbook to decode safely, so
        they are left out rather than risk inserting a mis-specified SKU.
    """
    already_seeded = conn.execute(
        "SELECT 1 FROM global_setting WHERE key='full_import_h136_v4_seeded'"
    ).fetchone()
    if already_seeded:
        return

    with open(os.path.join(BASE_DIR, "data", "full_import_h136.json"), encoding="utf-8") as f:
        data = json.load(f)

    # 1) Material rates (resin + packaging), except pe_bag (see docstring).
    for key, value in data["material_rates"].items():
        conn.execute("UPDATE material_rate SET value=? WHERE material_key=?", (value, key))
    conn.commit()

    # 2) BOM: full replace (both roll tiers, all stretch multipliers x microns).
    conn.execute("DELETE FROM bom_row")
    for b in data["bom_rows"]:
        conn.execute(
            """INSERT OR IGNORE INTO bom_row
               (stretch_multiplier, micron, roll_tier, exceed3518, exceed3812, exceedxp,
                vista6000, enable, ld258, vista6202)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (b["stretch_multiplier"], b["micron"], b["roll_tier"], b["exceed3518"], b["exceed3812"],
             b["exceedxp"], b["vista6000"], b["enable"], b["ld258"], b["vista6202"]),
        )
    conn.commit()

    # 3) Products: update existing (match by stretch_ability+micron+roll_weight_kg)
    # or insert new jumbo-roll SKUs the Stretch sheet lists that the catalog is
    # missing. fob_usd_kg/cfr_usd_kg come straight from the sheet; ex_work_usd_kg
    # is left for recalculate_all_products() at the end (never hardcoded from
    # the sheet's own EX-Work column, which is this app's job to compute).
    inserted_products = []
    updated_products = 0
    for sp in data["stretch_products"]:
        micron_str = str(int(sp["micron"])) if float(sp["micron"]).is_integer() else str(sp["micron"])
        row = conn.execute(
            "SELECT id FROM product WHERE stretch_ability=? AND micron=? AND roll_weight_kg=?",
            (sp["stretch_ability"], micron_str, sp["roll_weight_kg"]),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE product SET fob_usd_kg=?, cfr_usd_kg=? WHERE id=?",
                (sp["fob_usd_kg"], sp["cfr_usd_kg"], row["id"]),
            )
            updated_products += 1
        else:
            conn.execute(
                """INSERT INTO product
                   (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                    roll_weight_kg, core_weight_kg, width_mm, ex_work_usd_kg, fob_usd_kg, cfr_usd_kg)
                   VALUES (?,?,?,?,?,?,?,?,?,0,?,?)""",
                (sp["stretch_ability"], micron_str, "Standard", "Automatic", "Transparent",
                 sp["rolls_per_pallet"], sp["roll_weight_kg"], 1.8, 500,
                 sp["fob_usd_kg"], sp["cfr_usd_kg"]),
            )
            inserted_products.append(f"{sp['stretch_ability']} / {micron_str}mic / {sp['roll_weight_kg']}kg")
    conn.commit()

    # 4) Electricity (kW/ton) and production capacity (tons/day): full replace.
    conn.execute("DELETE FROM electricity_power")
    for micron, roll_type, kw in data["electricity_power"]:
        conn.execute(
            "INSERT OR IGNORE INTO electricity_power (micron, roll_type, kw_per_ton) VALUES (?,?,?)",
            (micron, roll_type, kw),
        )
    conn.execute("DELETE FROM production_capacity")
    for micron, roll_type, tons in data["production_capacity"]:
        conn.execute(
            "INSERT OR IGNORE INTO production_capacity (micron, roll_type, tons_per_day) VALUES (?,?,?)",
            (micron, roll_type, tons),
        )
    conn.commit()

    # 5) Fixed cost items: keyed UPDATE by name where it already exists (so the
    # labor-synced 'اجور عمال الانتاج' / 'اضافي عمال الانتاج' rows, which are
    # NOT in this sheet-sourced list, are left untouched and still driven live
    # by sync_labor_to_fixed_costs()); INSERT any sheet line item that's new.
    for category, name, value in data["fixed_cost_items"]:
        row = conn.execute(
            "SELECT id FROM fixed_cost_item WHERE name=? AND category=?", (name, category)
        ).fetchone()
        if row:
            conn.execute("UPDATE fixed_cost_item SET value_egp=? WHERE id=?", (value, row["id"]))
        else:
            conn.execute(
                "INSERT INTO fixed_cost_item (category, name, value_egp) VALUES (?,?,?)",
                (category, name, value),
            )
    conn.commit()

    # 6) Labor roster: update base wage + increase rate for each existing
    # employee (matched by name -- every name in this sheet already exists in
    # the roster). See docstring for why 'Labor Jan 2026' is not used here.
    for name, role, base, rate in data["labor_employees"]:
        conn.execute(
            "UPDATE labor_employee SET role=?, base_2023_egp=?, increase_rate=? WHERE name=?",
            (role, base, rate, name),
        )
    conn.commit()
    from . import cost_engine
    cost_engine.sync_labor_to_fixed_costs(conn)

    # 7) Factors: update the Automatic/UVI columns only (see docstring for why
    # regid/uv_regid are left alone).
    for (country_class, customer_class, roll_size, auto_std, auto_pow, auto_pp,
         uvi_std, uvi_pow, uvi_pp) in data["factors"]:
        conn.execute(
            """UPDATE factor SET automatic_standard=?, automatic_power=?, automatic_power_plus=?,
               uvi_standard=?, uvi_power=?, uvi_power_plus=?
               WHERE country_class=? AND customer_class=? AND roll_size=?""",
            (auto_std, auto_pow, auto_pp, uvi_std, uvi_pow, uvi_pp,
             country_class, customer_class, roll_size),
        )
    conn.commit()

    # 8) Pallet component: reconcile quantities on existing packing_keys that
    # differ from this newer sheet, and seed the two extra boxed variants
    # (12m 350% and the 15/17m-350%/35m-250% block) as inert, available rows
    # -- not wired to any product's packaging_group (owner confirmed only the
    # 12-micron/300% product uses special box packaging).
    for packing_key, updates in data["pallet_component_updates"].items():
        set_clause = ", ".join(f"{col}=?" for col in updates)
        conn.execute(
            f"UPDATE pallet_component SET {set_clause} WHERE packing_key=?",
            (*updates.values(), packing_key),
        )
    for (packing_key, label, pallet_size_label, cardboard, cap, corrugated, stretch_kg, box_qty,
         pe_bag_kg) in data["pallet_component_inserts"]:
        conn.execute(
            """INSERT OR IGNORE INTO pallet_component
               (packing_key, label, pallet_size_label, pallet_qty, cardboard_qty, cap_qty, corrugated_kg,
                stretch_kg, box_qty, rolls_per_box, cartoon_angle_qty, scotch_tape_qty, air_bag_qty, pe_bag_qty)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?)""",
            (packing_key, label, pallet_size_label, cardboard, cap, corrugated, stretch_kg, box_qty, pe_bag_kg),
        )
    conn.commit()

    # 9) Pre-Stretch packaging totals (see prestretch_packaging_noboxes_usd /
    # prestretch_packaging_boxes_usd help text) -- updated to this sheet's
    # 'Pallet component'!Q11/V11 totals.
    for key, value in data["global_setting_updates"].items():
        conn.execute("UPDATE global_setting SET value=? WHERE key=?", (value, key))
    conn.commit()

    conn.execute(
        "INSERT INTO global_setting (key, label, value, help) VALUES (?, ?, ?, ?)",
        ("full_import_h136_v4_seeded", "Full H1.36 workbook import v4 seeded (internal marker)", 1,
         "Internal marker: material rates, BOM, products, electricity/production-capacity, fixed "
         "costs, labor roster, factors and pallet-component quantities have been refreshed from "
         "'Stretch_Export_Pricing_H1.36'. Do not delete this row -- it stops the one-time import "
         "from running again and overwriting manual admin edits made after this boot. "
         f"New products inserted at seed time: {len(inserted_products)}; existing products "
         f"updated: {updated_products}."),
    )
    conn.commit()
    cost_engine.recalculate_all_products(conn)


# (film_type, micron_min, micron_max, packing_type, roll_size, margin_pct)
# Transcribed from 5 screenshots of the reference app ("Hesham Natora"'s
# stretch-pricing-app) Margin Factors admin table. One row --
# UVI_12m_Standard / 12-40 / Automatic / Jumbo Roll size -- was not directly
# visible (cut off between two screenshots) and was inferred at 8.00 by
# exact pattern-match against the fully-visible UVI_6m_Standard block below,
# which has an identical shape; every other film_type in this table follows
# the same pattern (every Automatic tier has both a Standard-Roll-size and a
# Jumbo-Roll-size row at the same margin%) with zero exceptions. See
# db._seed_margin_factor_v5's docstring.
MARGIN_FACTOR_ROWS = [
    # Power
    ("Power", 10, 12, "Automatic", "Standard Roll size", 17.00),
    ("Power", 10, 12, "Automatic", "Jumbo Roll size", 17.00),
    ("Power", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("Power", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("Power", 10, 12, "Manual", "Manual Roll size", 18.00),
    ("Power", 15, 40, "Manual", "Manual Roll size", 14.00),
    # Power_Plus (identical shape to Power)
    ("Power_Plus", 10, 12, "Automatic", "Standard Roll size", 17.00),
    ("Power_Plus", 10, 12, "Automatic", "Jumbo Roll size", 17.00),
    ("Power_Plus", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("Power_Plus", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("Power_Plus", 10, 12, "Manual", "Manual Roll size", 18.00),
    ("Power_Plus", 15, 40, "Manual", "Manual Roll size", 14.00),
    # Prestretch
    ("Prestretch", 5, 40, "Pre-stretch (No Box)", "Prestretch Roll size", 0.00),
    ("Prestretch", 5, 40, "Pre-stretch (Box)", "Prestretch Roll size", 0.00),
    # Regular_Rigid
    ("Regular_Rigid", 8, 12, "Automatic", "Standard Roll size", 20.00),
    ("Regular_Rigid", 8, 12, "Automatic", "Jumbo Roll size", 20.00),
    ("Regular_Rigid", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("Regular_Rigid", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("Regular_Rigid", 8, 12, "Manual", "Manual Roll size", 21.00),
    ("Regular_Rigid", 15, 40, "Manual", "Manual Roll size", 15.00),
    # Standard
    ("Standard", 8, 9, "Automatic", "Standard Roll size", 17.00),
    ("Standard", 8, 9, "Automatic", "Jumbo Roll size", 17.00),
    ("Standard", 10, 12, "Automatic", "Standard Roll size", 15.00),
    ("Standard", 10, 12, "Automatic", "Jumbo Roll size", 15.00),
    ("Standard", 15, 40, "Automatic", "Standard Roll size", 8.00),
    ("Standard", 15, 40, "Automatic", "Jumbo Roll size", 8.00),
    ("Standard", 8, 9, "Manual", "Manual Roll size", 18.00),
    ("Standard", 10, 12, "Manual", "Manual Roll size", 16.00),
    ("Standard", 15, 40, "Manual", "Manual Roll size", 10.00),
    # Super_Rigid (identical shape to Regular_Rigid)
    ("Super_Rigid", 8, 12, "Automatic", "Standard Roll size", 20.00),
    ("Super_Rigid", 8, 12, "Automatic", "Jumbo Roll size", 20.00),
    ("Super_Rigid", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("Super_Rigid", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("Super_Rigid", 8, 12, "Manual", "Manual Roll size", 21.00),
    ("Super_Rigid", 15, 40, "Manual", "Manual Roll size", 15.00),
    # v53 -- UVI_12m_Power/Power_Plus/Standard and UV_Rigid margins, below,
    # were originally transcribed (v5, see module comment above) from 5
    # screenshots of a REFERENCE app's Margin Factors table -- not from the
    # owner's own source sheets -- and used a different, narrower set of
    # micron cutoffs than their own non-UV counterpart (Power/Power_Plus/
    # Standard/Regular_Rigid/Super_Rigid above). That mismatch meant turning
    # the UV checkbox on could silently DROP the margin (e.g. Standard at 12
    # micron: 15% -> 8%) and even fall through to 0% margin entirely for
    # micron 8/9/11 (no matching row at all) -- so UV could make a quote
    # CHEAPER than the same line without UV, the opposite of what UV is
    # supposed to do. The owner confirmed explicitly: UV must always be
    # ADDITIVE (only ever raise the price), for every product, at every
    # micron. Fixed by making every UVI_*/UV_Rigid film_type mirror its own
    # non-UV counterpart's micron buckets AND percentages exactly, so the
    # margin itself never drops when UV is picked -- combined with the
    # UVI_FRACTION material-cost addition (cost_engine.UVI_FRACTION, applied
    # regardless of margin), the price is then always strictly higher with
    # UV than without it. See db._fix_uvi_margin_v53() for the one-time
    # migration that applies this same fix to an already-seeded live DB.
    # UVI_12m_Power (mirrors Power exactly)
    ("UVI_12m_Power", 10, 12, "Automatic", "Standard Roll size", 17.00),
    ("UVI_12m_Power", 10, 12, "Automatic", "Jumbo Roll size", 17.00),
    ("UVI_12m_Power", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("UVI_12m_Power", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("UVI_12m_Power", 10, 12, "Manual", "Manual Roll size", 18.00),
    ("UVI_12m_Power", 15, 40, "Manual", "Manual Roll size", 14.00),
    # UVI_12m_Power_Plus (mirrors Power_Plus exactly)
    ("UVI_12m_Power_Plus", 10, 12, "Automatic", "Standard Roll size", 17.00),
    ("UVI_12m_Power_Plus", 10, 12, "Automatic", "Jumbo Roll size", 17.00),
    ("UVI_12m_Power_Plus", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("UVI_12m_Power_Plus", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("UVI_12m_Power_Plus", 10, 12, "Manual", "Manual Roll size", 18.00),
    ("UVI_12m_Power_Plus", 15, 40, "Manual", "Manual Roll size", 14.00),
    # UVI_12m_Standard (mirrors Standard exactly)
    ("UVI_12m_Standard", 8, 9, "Automatic", "Standard Roll size", 17.00),
    ("UVI_12m_Standard", 8, 9, "Automatic", "Jumbo Roll size", 17.00),
    ("UVI_12m_Standard", 10, 12, "Automatic", "Standard Roll size", 15.00),
    ("UVI_12m_Standard", 10, 12, "Automatic", "Jumbo Roll size", 15.00),
    ("UVI_12m_Standard", 15, 40, "Automatic", "Standard Roll size", 8.00),
    ("UVI_12m_Standard", 15, 40, "Automatic", "Jumbo Roll size", 8.00),
    ("UVI_12m_Standard", 8, 9, "Manual", "Manual Roll size", 18.00),
    ("UVI_12m_Standard", 10, 12, "Manual", "Manual Roll size", 16.00),
    ("UVI_12m_Standard", 15, 40, "Manual", "Manual Roll size", 10.00),
    # UVI_6m_Power (mirrors Power exactly)
    ("UVI_6m_Power", 10, 12, "Automatic", "Standard Roll size", 17.00),
    ("UVI_6m_Power", 10, 12, "Automatic", "Jumbo Roll size", 17.00),
    ("UVI_6m_Power", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("UVI_6m_Power", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("UVI_6m_Power", 10, 12, "Manual", "Manual Roll size", 18.00),
    ("UVI_6m_Power", 15, 40, "Manual", "Manual Roll size", 14.00),
    # UVI_6m_Power_Plus (mirrors Power_Plus exactly)
    ("UVI_6m_Power_Plus", 10, 12, "Automatic", "Standard Roll size", 17.00),
    ("UVI_6m_Power_Plus", 10, 12, "Automatic", "Jumbo Roll size", 17.00),
    ("UVI_6m_Power_Plus", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("UVI_6m_Power_Plus", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("UVI_6m_Power_Plus", 10, 12, "Manual", "Manual Roll size", 18.00),
    ("UVI_6m_Power_Plus", 15, 40, "Manual", "Manual Roll size", 14.00),
    # UVI_6m_Standard (mirrors Standard exactly)
    ("UVI_6m_Standard", 8, 9, "Automatic", "Standard Roll size", 17.00),
    ("UVI_6m_Standard", 8, 9, "Automatic", "Jumbo Roll size", 17.00),
    ("UVI_6m_Standard", 10, 12, "Automatic", "Standard Roll size", 15.00),
    ("UVI_6m_Standard", 10, 12, "Automatic", "Jumbo Roll size", 15.00),
    ("UVI_6m_Standard", 15, 40, "Automatic", "Standard Roll size", 8.00),
    ("UVI_6m_Standard", 15, 40, "Automatic", "Jumbo Roll size", 8.00),
    ("UVI_6m_Standard", 8, 9, "Manual", "Manual Roll size", 18.00),
    ("UVI_6m_Standard", 10, 12, "Manual", "Manual Roll size", 16.00),
    ("UVI_6m_Standard", 15, 40, "Manual", "Manual Roll size", 10.00),
    # UV_Rigid (mirrors Regular_Rigid/Super_Rigid exactly -- both already
    # numerically identical to each other, see cost_engine.roll_type_bucket())
    ("UV_Rigid", 8, 12, "Automatic", "Standard Roll size", 20.00),
    ("UV_Rigid", 8, 12, "Automatic", "Jumbo Roll size", 20.00),
    ("UV_Rigid", 15, 40, "Automatic", "Standard Roll size", 13.00),
    ("UV_Rigid", 15, 40, "Automatic", "Jumbo Roll size", 13.00),
    ("UV_Rigid", 8, 12, "Manual", "Manual Roll size", 21.00),
    ("UV_Rigid", 15, 40, "Manual", "Manual Roll size", 15.00),
]


# v6 bugfix: _seed_cost_engine_data() (first-ever boot only, table-empty
# gated) seeds these 43 Arabic-labeled `fixed_cost_item` rows from the
# OLDER cost_seed.json. _seed_full_import_v4() later seeds 40 English-
# labeled rows for the SAME real-world line items from the H1.36 sheet's
# own "Fixed" tab, matched by (name, category) -- since the names never
# match (different language), it always INSERTS instead of updating, so
# every boot ends up with BOTH the stale Arabic rows AND the current
# English rows for production/selling/admin, and total_fixed_cost_egp()
# (a plain SUM over every row) silently double-counts almost the entire
# fixed-cost base -- confirmed against the sheet: the 40 English rows sum
# to EXACTLY the sheet's own "Total Fixed Cost" (2,916,751.81 EGP), while
# the 43 stale Arabic rows add another 1,692,079.83 EGP on top that
# shouldn't be there, inflating every single product's EX-Work cost (and
# therefore every quoted price) by ~58% on the fixed-cost component.
# The 3 'financial' rows (bank charges/interest) have no English H1.36
# counterpart at all because the current "Fixed" tab explicitly zeroes
# out its whole Finance Costs section (owner's own note on that tab:
# "removed thanks to good management of the raw-material interest file,
# offset by credit interest") -- so those 3 are stale too, not just
# renamed, and are deleted outright rather than carried forward.
# This function deletes exactly those 43 legacy rows (idempotent -- a
# fresh boot reproduces the SAME final state every time on Render's
# no-persistent-disk free tier, so this must be a permanent code fix,
# not a one-off DB patch) and leaves every English H1.36 row untouched.
LEGACY_FIXED_COST_ITEMS_V4 = [
    ("production", "كهرباء بريموباك"),
    ("production", "مياه"),
    ("production", "اجور عمال الانتاج"),
    ("production", "اضافي عمال الانتاج"),
    ("production", "مرتبات صناعية غ.م."),
    ("production", "اضافي مرتبات صناعية غ.م."),
    ("production", "تأمينات اجتماعية"),
    ("production", "انتقالات وماموريات"),
    ("production", "علاج"),
    ("production", "مكافأت"),
    ("production", "اعياد و مناسبات"),
    ("production", 'مصروف الاهلاك "عام"'),
    ("production", 'مصروف الاهلاك "بريموباك"'),
    ("production", 'مصروف الاهلاك "Uni Tech"'),
    ("production", "ايجار مخزن"),
    ("production", "صيانة و قطع غيار بريموباك"),
    ("production", "م.سيارات"),
    ("production", "م.كلاركات"),
    ("production", "امن صناعي"),
    ("production", "اخرى"),
    ("selling", "عمولات و مصروفات بنكية"),
    ("selling", "رسوم و تراخيص"),
    ("selling", "معارض"),
    ("selling", "اخرى"),
    ("selling", "رواتب بيع"),
    ("selling", "تأمينات اجتماعية"),
    ("selling", "اخرى"),
    ("admin", "رواتب ادارة"),
    ("admin", "اضافي"),
    ("admin", "تأمينات اجتماعية"),
    ("admin", "انتقالات وماموريات"),
    ("admin", "اعياد و مناسبات"),
    ("admin", "مكافأت و حوافز"),
    ("admin", "رسوم و تراخيص"),
    ("admin", "اتعاب و استشارات"),
    ("admin", "الادارة العليا"),
    ("admin", "مرافق"),
    ("admin", "ايجار"),
    ("admin", "اخرى"),
    ("admin", "مصروف الاهلاك الاداري"),
    ("financial", "عمولات و مصروفات بنكية"),
    ("financial", "دمغات و فوائد"),
    ("financial", "فوائد و دمغات بريموباك"),
]


def _fix_fixed_cost_duplication_v6(conn):
    from . import cost_engine

    changed = False
    for category, name in LEGACY_FIXED_COST_ITEMS_V4:
        cur = conn.execute(
            "DELETE FROM fixed_cost_item WHERE category=? AND name=?", (category, name)
        )
        if cur.rowcount:
            changed = True
    conn.commit()
    if changed:
        cost_engine.recalculate_all_products(conn)


# v21.1 bugfix: two global_setting constants were left at their OLDER,
# now-stale values from a prior workbook version (both were seeded once,
# on the very first-ever boot, from cost_seed.json -- the H1.36 full
# import's global_setting_updates only touched the 2 pre-stretch packaging
# totals, never these two) even though the reference H1.36 workbook itself
# has since changed them:
#   - scrap_interest_factor: DB had 1.031, but every current Stretch-sheet
#     material-cost formula (e.g. Stretch!T39: "=K39*J39*'Material
#     pricing'!$C$4/1000*1.01*1.04") multiplies by 1.04, not 1.031.
#   - electricity_variable_tariff_egp_per_kwh: DB had 1.32, but the H1.36
#     'Electricity' sheet's own B6 "Variable Tariff" cell is now 2.8.
# Both were confirmed by reproducing the sheet's own EX-Work Cost (KG) for
# a specific line (17mic/300%/50kg/Automatic, gross weight) by hand and
# finding the DB's cached rates undershot the sheet's own computed value
# on both the material and conversion-cost components. Corrected here,
# unconditionally, since (like _fix_fixed_cost_duplication_v6) Render's
# free tier has no persistent disk and a fresh boot would otherwise reseed
# the same stale values from cost_seed.json every time.
STALE_GLOBAL_SETTINGS_V7 = {
    "scrap_interest_factor": 1.04,
    "electricity_variable_tariff_egp_per_kwh": 2.8,
}


def _fix_stale_global_settings_v7(conn):
    from . import cost_engine

    changed = False
    for key, correct_value in STALE_GLOBAL_SETTINGS_V7.items():
        row = conn.execute("SELECT value FROM global_setting WHERE key=?", (key,)).fetchone()
        if row is not None and row["value"] != correct_value:
            conn.execute("UPDATE global_setting SET value=? WHERE key=?", (correct_value, key))
            changed = True
    conn.execute(
        """UPDATE global_setting SET help=?
           WHERE key='scrap_interest_factor'""",
        ("Stretch sheet material-cost formulas multiply by 1.04 (handling/scrap allowance).",),
    )
    conn.commit()
    if changed:
        cost_engine.recalculate_all_products(conn)


# v28 -- 3 material_rate values were seeded from a stale/intermediate import
# (full_import_h136.json) and never matched the reference app's own admin
# Settings -> Materials screen (ground truth, confirmed against the
# reference app's sales_manager account). Corrected here, not just in
# cost_seed.json, because Render's free tier has no persistent disk: every
# boot runs the full seed chain from scratch, and a later seed step
# (_seed_full_import_v4) already overwrote cost_seed.json's original values
# with different-but-still-wrong ones, so cost_seed.json alone would not
# fix the values the app actually serves.
STALE_MATERIAL_RATES_V8 = {
    "core": 35,     # packaging, kilo -- was 30
    "enable": 1490,  # resin, ton -- was 1290
    "cap": 50,      # packaging, piece ("Cap 1100~1200") -- was 46
}


def _fix_stale_material_rates_v8(conn):
    from . import cost_engine

    changed = False
    for key, correct_value in STALE_MATERIAL_RATES_V8.items():
        row = conn.execute("SELECT value FROM material_rate WHERE material_key=?", (key,)).fetchone()
        if row is not None and row["value"] != correct_value:
            conn.execute("UPDATE material_rate SET value=? WHERE material_key=?", (correct_value, key))
            changed = True
    conn.commit()
    if changed:
        cost_engine.recalculate_all_products(conn)


def _seed_margin_factor_v5(conn):
    """v18 -- owner-confirmed full replacement of the country_class x
    customer_class x roll_size `factor` margin system with the reference
    app's micron x film_type x packing_type x roll_size `margin_factor`
    system (see cost_engine.margin_pct_for()). One-time seed, gated behind
    a global_setting marker like every other one-time refresh in this file,
    so a later restart never re-runs this and clobbers a manual admin edit.
    The old `factor` table and its country_class/customer_class columns on
    `quotation` are deliberately left in the schema (harmless dead data /
    too disruptive to drop from a live table) -- they are simply no longer
    read anywhere in the pricing flow, PDF or quote-builder UI."""
    already_seeded = conn.execute(
        "SELECT 1 FROM global_setting WHERE key='margin_factor_v5_seeded'"
    ).fetchone()
    if already_seeded:
        return

    for film_type, micron_min, micron_max, packing_type, roll_size, margin_pct in MARGIN_FACTOR_ROWS:
        conn.execute(
            """INSERT INTO margin_factor
               (film_type, micron_min, micron_max, packing_type, roll_size, margin_pct)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (film_type, micron_min, micron_max, packing_type, roll_size, margin_pct),
        )
    conn.commit()

    conn.execute(
        "INSERT INTO global_setting (key, label, value, help) VALUES (?, ?, ?, ?)",
        ("margin_factor_v5_seeded", "Margin factor v5 seeded (internal marker)", 1,
         "Internal marker: the micron x film_type x packing_type x roll_size margin_factor table has "
         "been seeded from the reference app's Margin Factors admin screen, replacing the old "
         "country_class x customer_class x roll_size factor table for all pricing. Do not delete "
         "this row -- it stops the one-time seed from running again and duplicating rows or "
         "overwriting manual admin edits made after this boot."),
    )
    conn.commit()


# v53 -- the 7 UVI_*/UV_Rigid film_types whose margin rows this replaces,
# so an already-seeded live DB can be patched without touching anything else
# an admin may have since edited in the margin_factor table.
UVI_MARGIN_FIX_V53_FILM_TYPES = [
    "UVI_12m_Power", "UVI_12m_Power_Plus", "UVI_12m_Standard",
    "UVI_6m_Power", "UVI_6m_Power_Plus", "UVI_6m_Standard", "UV_Rigid",
]


def _fix_uvi_margin_v53(conn):
    """One-time fix for a real pricing bug the owner reported and confirmed:
    turning the UV checkbox on could make a quote CHEAPER, not more
    expensive, at several microns (e.g. Standard at 12 micron: margin
    dropped from 15% to 8% the moment UV was picked; 8/9/11 micron fell
    through to 0% margin entirely -- no margin_factor row matched at all).
    Root cause: MARGIN_FACTOR_ROWS' UVI_*/UV_Rigid rows (see that list's own
    comment) were transcribed from a REFERENCE app's screenshots, not the
    owner's own sheets, and used different/narrower micron cutoffs than
    their own non-UV counterpart film_type. Confirmed with the owner: UV
    must always be ADDITIVE -- only ever raise the price, for every
    product, at every micron. Fixed by replacing just these 7 film_types'
    rows with ones that mirror their own non-UV counterpart's micron
    buckets and percentages exactly (see MARGIN_FACTOR_ROWS), so the margin
    itself never drops when UV is on; combined with the UVI_FRACTION
    material-cost addition already applied regardless, the price is then
    always strictly higher with UV than without. One-time, gated behind a
    global_setting marker like every other fix in this file, so it patches
    an already-seeded live DB exactly once without touching any OTHER
    film_type's margin row an admin may have since edited by hand."""
    already_fixed = conn.execute(
        "SELECT 1 FROM global_setting WHERE key='uvi_margin_fix_v53_applied'"
    ).fetchone()
    if already_fixed:
        return

    placeholders = ",".join("?" for _ in UVI_MARGIN_FIX_V53_FILM_TYPES)
    conn.execute(
        f"DELETE FROM margin_factor WHERE film_type IN ({placeholders})",
        UVI_MARGIN_FIX_V53_FILM_TYPES,
    )
    for film_type, micron_min, micron_max, packing_type, roll_size, margin_pct in MARGIN_FACTOR_ROWS:
        if film_type not in UVI_MARGIN_FIX_V53_FILM_TYPES:
            continue
        conn.execute(
            """INSERT INTO margin_factor
               (film_type, micron_min, micron_max, packing_type, roll_size, margin_pct)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (film_type, micron_min, micron_max, packing_type, roll_size, margin_pct),
        )
    conn.commit()

    conn.execute(
        "INSERT INTO global_setting (key, label, value, help) VALUES (?, ?, ?, ?)",
        ("uvi_margin_fix_v53_applied", "UVI margin fix v53 applied (internal marker)", 1,
         "Internal marker: the UVI_12m_Power/Power_Plus/Standard, UVI_6m_Power/Power_Plus/Standard "
         "and UV_Rigid margin_factor rows were replaced so UV is always additive (never lowers the "
         "margin vs. the same product without UV) -- see db._fix_uvi_margin_v53()'s docstring. Do not "
         "delete this row -- it stops the one-time fix from running again and overwriting a manual "
         "admin edit made after this boot."),
    )
    conn.commit()


# v63 -- (stretch_ability, micron) pairs where the catalog ended up with
# TWO product rows for the same spec: an older "standard" roll (16kg,
# width_mm blank) seeded from the original catalog, and a newer "jumbo"
# roll (50kg, width_mm=500) added later by _seed_full_import_v4() when it
# imported the H1.36 workbook -- that import matched existing rows by
# stretch_ability+micron+roll_weight_kg, so a jumbo row with no matching
# roll_weight_kg on file was inserted as a brand-new row instead of
# replacing the stale one. The owner confirmed (after the H1.36 workbook
# shows EVERY 200/250/300/350% Power micron as jumbo-only, with no lighter
# variant left) that the jumbo row is the current, correct one and the
# older "standard" row for these same specs is simply stale -- and that
# going forward there should be ONE catalog row per spec, with the actual
# roll weight for a given order typed into the quotation line's own Roll
# kg field (already supported -- see readLines()'s custom_roll_weight_kg
# in pricing.html and cost_engine.get_bom_row()'s roll_tier lookup, which
# already picks the "jumbo" vs "standard" BOM bracket from whatever roll
# weight is actually in effect for that line, catalog-selection or
# override, so nothing else has to change for this to keep working).
DEDUPE_STALE_JUMBO_PRODUCTS_V63 = [
    ("200% Power", "17"), ("200% Power", "20"), ("200% Power", "23"),
    ("250% Power", "17"), ("250% Power", "20"), ("250% Power", "23"),
    ("300% (Power plus)", "17"), ("300% (Power plus)", "20"), ("300% (Power plus)", "23"),
    ("350% (Power plus)", "12"), ("350% (Power plus)", "15"),
    ("350% (Power plus)", "17"), ("350% (Power plus)", "23"),
]


def _dedupe_stale_jumbo_products_v63(conn):
    """One-time cleanup for the stale-duplicate-product issue the owner
    reported (two catalog rows for the same micron+stretch_ability, priced
    differently) -- see DEDUPE_STALE_JUMBO_PRODUCTS_V63's comment above for
    the root cause and the owner's confirmed resolution. For each listed
    pair, if BOTH a <=25kg row and a >25kg (jumbo) row still exist, the
    <=25kg row is deleted -- but ONLY if nothing still points at it: no
    saved quotation_line references it (a past quote must keep showing
    exactly what it showed when it was quoted) and no Pre-Stretch product
    uses it as its prestretch_source_product_id (Pre-Stretch pricing is
    sourced from the jumbo rows specifically -- see
    JUMBO_PRESTRETCH_PRECURSOR_PRODUCTS/PRESTRETCH_PRODUCTS above -- so
    those were never at risk here, this check is just a defensive
    safety net). A pair with anything other than exactly one <=25kg row
    and one >25kg row (already cleaned up, or hand-edited into some other
    shape by an admin since) is left alone rather than guessed at.
    Gated behind a global_setting marker like every other one-time fix in
    this file, so it runs exactly once against an already-deployed, already-
    seeded live DB and never re-touches a row an admin has since added or
    edited by hand."""
    already_run = conn.execute(
        "SELECT 1 FROM global_setting WHERE key='dedupe_stale_jumbo_products_v63'"
    ).fetchone()
    if already_run:
        return

    deleted = []
    kept_both = []
    for stretch_ability, micron in DEDUPE_STALE_JUMBO_PRODUCTS_V63:
        rows = conn.execute(
            "SELECT * FROM product WHERE stretch_ability=? AND micron=?",
            (stretch_ability, micron),
        ).fetchall()
        standard_rows = [r for r in rows if (r["roll_weight_kg"] or 0) <= 25]
        jumbo_rows = [r for r in rows if (r["roll_weight_kg"] or 0) > 25]
        if len(standard_rows) != 1 or len(jumbo_rows) != 1:
            continue
        old_row = standard_rows[0]
        still_used = conn.execute(
            "SELECT COUNT(*) c FROM quotation_line WHERE product_id=?", (old_row["id"],)
        ).fetchone()["c"]
        still_sourced = conn.execute(
            "SELECT COUNT(*) c FROM product WHERE prestretch_source_product_id=?", (old_row["id"],)
        ).fetchone()["c"]
        if still_used or still_sourced:
            kept_both.append((stretch_ability, micron, old_row["id"]))
            continue
        conn.execute("DELETE FROM product WHERE id=?", (old_row["id"],))
        deleted.append((stretch_ability, micron, old_row["id"]))
    conn.commit()

    note = f"Deleted stale rows: {deleted}. Kept both (still referenced): {kept_both}."
    conn.execute(
        "INSERT INTO global_setting (key, label, value, help) VALUES (?, ?, ?, ?)",
        ("dedupe_stale_jumbo_products_v63", "Stale jumbo-duplicate product cleanup v63 (internal marker)", 1,
         "Internal marker: the one-time stale-standard-vs-jumbo product row cleanup has run -- see "
         "db._dedupe_stale_jumbo_products_v63()'s docstring. Do not delete this row -- it stops the "
         "cleanup from running again. " + note),
    )
    conn.commit()


# v64 -- fills real micron gaps in the catalog, confirmed against the
# owner's own H1.36 workbook data ALREADY imported into this app (bom_row's
# C4/material-composition figures exist for every one of these microns --
# see full_import_h136.json -- it was only the PRODUCT catalog's physical
# roll-spec row that was missing, not the pricing data behind it), never
# guessed numbers:
#
# 150% Standard, microns 8/9/10/15/25/30/40: the workbook's own roll-spec
# columns (Width/Rolls-per-pallet/Roll weight/Core weight) are blank for
# every one of these EXCEPT micron 23 -- but the 4 microns already in this
# app's catalog for this exact grade (12/17/20/23) all share the IDENTICAL
# real roll spec (16kg roll, 46 rolls/pallet, 1.8kg core, Standard pallet,
# Automatic, Transparent -- see seed_data.json), confirming this whole
# grade uses one uniform physical roll regardless of micron (only the
# material composition/C4, i.e. the price, varies by micron -- exactly
# what bom_row already has for every one of these). So the missing
# microns get that same confirmed-real geometry, not an invented one.
STANDARD_150_MISSING_MICRONS = ["8", "9", "10", "15", "25", "30", "40"]
STANDARD_150_GEOMETRY = dict(pallet_size="Standard", auto_manual="Automatic", color="Transparent",
                              rolls_per_pallet=46, roll_weight_kg=16, core_weight_kg=1.8, width_mm=None)

# 300% (Power plus), micron 15: the workbook's roll-spec columns are blank
# for this ONE row, but its bom_row/C4 figure IS present. Every other
# micron in this same grade from 17 up (17/20/23/25/30/40) uses the jumbo
# roll spec (50kg roll, 500mm width, 16 rolls/pallet, 1.8kg core); only
# micron 12 is the odd one out at the old standard spec. 15 sits between
# them, so it's seeded as jumbo, matching the grade's dominant pattern --
# flagged to the owner as the one genuine judgment call in this batch (vs.
# the 150% Standard microns above, which reuse an already-100%-consistent
# spec) in case her actual roll for this micron is the older standard size
# instead.
POWER_PLUS_300_MICRON_15_GEOMETRY = dict(stretch_ability="300% (Power plus)", micron="15",
                                          pallet_size="Standard", auto_manual="Automatic", color="Transparent",
                                          rolls_per_pallet=16, roll_weight_kg=50, core_weight_kg=1.8, width_mm=500)


def _seed_confirmed_micron_gaps_v64(conn):
    """One-time fill for the two confirmed-real micron gaps described above
    -- see STANDARD_150_MISSING_MICRONS/POWER_PLUS_300_MICRON_15_GEOMETRY's
    comments. Deliberately does NOT touch the 'Special (Power Plus)' grade
    (Stretch!55-62 in the H1.36 workbook) or the remaining Rigid gaps
    (Regular 15/17/20/23, Super/Premium 8/17/20/23) -- neither has ANY real
    roll-spec or usable material-composition data anywhere in either
    workbook on file (the 'Special (Power Plus)' rows' own C4 is a flat 1
    for every micron -- a blank template value, not a real composition),
    so there is nothing on file to seed them from; those still need the
    owner's own numbers. Idempotent/per-row like every other seeding
    function in this file, and gated behind a global_setting marker so it
    runs its live-recompute step exactly once."""
    from . import cost_engine

    already_run = conn.execute(
        "SELECT 1 FROM global_setting WHERE key='confirmed_micron_gaps_v64_seeded'"
    ).fetchone()
    if already_run:
        return

    inserted = []
    for micron in STANDARD_150_MISSING_MICRONS:
        exists = conn.execute(
            "SELECT id FROM product WHERE stretch_ability='150% Standard' AND micron=?", (micron,)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO product
               (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                roll_weight_kg, core_weight_kg, width_mm, ex_work_usd_kg)
               VALUES ('150% Standard', ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (micron, STANDARD_150_GEOMETRY["pallet_size"], STANDARD_150_GEOMETRY["auto_manual"],
             STANDARD_150_GEOMETRY["color"], STANDARD_150_GEOMETRY["rolls_per_pallet"],
             STANDARD_150_GEOMETRY["roll_weight_kg"], STANDARD_150_GEOMETRY["core_weight_kg"],
             STANDARD_150_GEOMETRY["width_mm"]),
        )
        inserted.append(("150% Standard", micron))

    g = POWER_PLUS_300_MICRON_15_GEOMETRY
    exists = conn.execute(
        "SELECT id FROM product WHERE stretch_ability=? AND micron=?", (g["stretch_ability"], g["micron"])
    ).fetchone()
    if not exists:
        conn.execute(
            """INSERT INTO product
               (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                roll_weight_kg, core_weight_kg, width_mm, ex_work_usd_kg)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (g["stretch_ability"], g["micron"], g["pallet_size"], g["auto_manual"], g["color"],
             g["rolls_per_pallet"], g["roll_weight_kg"], g["core_weight_kg"], g["width_mm"]),
        )
        inserted.append((g["stretch_ability"], g["micron"]))
    conn.commit()

    # Cache each newly-seeded row's live-computed EX-Work rate.
    for stretch_ability, micron in inserted:
        row = conn.execute(
            "SELECT * FROM product WHERE stretch_ability=? AND micron=?", (stretch_ability, micron)
        ).fetchone()
        if row:
            new_val = cost_engine.compute_ex_work_usd_kg(conn, row)
            conn.execute("UPDATE product SET ex_work_usd_kg=? WHERE id=?", (new_val, row["id"]))
    conn.commit()

    conn.execute(
        "INSERT INTO global_setting (key, label, value, help) VALUES (?, ?, ?, ?)",
        ("confirmed_micron_gaps_v64_seeded", "Confirmed micron-gap catalog fill v64 (internal marker)", 1,
         "Internal marker: the one-time fill of 150% Standard microns 8/9/10/15/25/30/40 and 300% "
         "(Power plus) micron 15 has run -- see db._seed_confirmed_micron_gaps_v64()'s docstring. Do "
         "not delete this row -- it stops the fill from running again and overwriting a manual admin "
         "edit made after this boot. Inserted: " + str(inserted)),
    )
    conn.commit()


def _seed_default_users(conn):
    """Idempotent per-username seeding (not gated on the whole table being
    empty) so new default accounts (e.g. pasquale/manuel, added later) get
    created on an already-deployed, already-seeded live DB too, without
    touching or duplicating any existing account."""
    # v39 -- hidden markup (percentage points), owner-requested for these
    # two accounts specifically -- not shown anywhere in their UI. v44:
    # generalized from Strap-only (strap_markup_pct) to also cover Stretch
    # Film (markup_mode/markup_value). v46: split into independent
    # Stretch/Strap pairs -- all four legacy/current columns are kept in
    # sync here for a brand-new DB so any code path sees the same value.
    strap_markup_by_username = {"manuel": 1.5, "pasquale": 1.5}
    for username, full_name, role, region, seller_type, adjustment in DEFAULT_USERS:
        exists = conn.execute("SELECT id FROM user WHERE username=?", (username,)).fetchone()
        if exists:
            continue
        markup_value = strap_markup_by_username.get(username, 0)
        conn.execute(
            """INSERT INTO user (username, full_name, password_hash, role, region,
                                  seller_type, price_adjustment_usd_kg, strap_markup_pct,
                                  markup_mode, markup_value,
                                  stretch_markup_mode, stretch_markup_value,
                                  strap_markup_mode, strap_markup_value)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (username, full_name, generate_password_hash("ChangeMe123!"), role, region,
             seller_type, adjustment, markup_value, "percent", markup_value,
             "percent", markup_value, "percent", markup_value),
        )
    conn.commit()


# ============================================================================
# v30 -- PET Strap / PP Strap product lines
# ============================================================================
# Reverse-engineered directly from the owner's own PET_Export_pricing_1.25.xlsx
# and PP_Export_pricing_1.32.xlsx cost sheets (Material cost / Fixed Cost /
# Electricity-Powers / PET-PP product tabs). See app/strap_pricing.py for the
# full formula chain these feed. Material rates use a namespaced key
# ("pet_"/"pp_" prefix) so they never collide with -- or accidentally get
# edited alongside -- Stretch Film's own same-named materials (Core, Box,
# Pallet, etc. are genuinely different physical items/suppliers with
# different rates for this line; confirmed by comparing the two sheets'
# values against the live material_rate table, which did not match).
#
# material_key, label, category, unit, value
STRAP_MATERIAL_RATES = [
    # -- PET --
    ("pet_c4", "PET: C4", "resin", "ton", 1190),
    ("pet_resin", "PET: PET Resin", "resin", "ton", 35000),
    ("pet_color", "PET: Color/Green S66", "resin", "ton", 150000),
    ("pet_core", "PET: Core", "packaging", "kilo", 40),
    ("pet_stretch", "PET: Stretch wrap", "packaging", "kilo", 60),
    ("pet_cardboard", "PET: Cardboard 400~500", "packaging", "piece", 20),
    ("pet_pallet", "PET: Pallet", "packaging", "piece", 500),
    ("pet_box", "PET: Box", "packaging", "piece", 40),
    ("pet_jwan", "PET: Jwan", "packaging", "piece", 5),
    # -- PP --
    ("pp_5032", "PP: 5032 PP", "resin", "ton", 1375),
    ("pp_coco3", "PP: COCO3", "resin", "ton", 23000),
    ("pp_recycled_colored", "PP: Recycled Colored PP", "resin", "ton", 36500),
    ("pp_recycled_pure", "PP: Recycled Pure PP", "resin", "ton", 32000),
    ("pp_color", "PP: Color", "resin", "ton", 172500),
    ("pp_core", "PP: Core", "packaging", "kilo", 42),
    ("pp_stretch", "PP: Stretch wrap", "packaging", "kilo", 60),
    ("pp_cardboard", "PP: Cardboard 400~500", "packaging", "piece", 20),
    ("pp_pallet", "PP: Pallet", "packaging", "piece", 470),
    ("pp_box", "PP: Box", "packaging", "piece", 42),
    ("pp_jwan", "PP: Jwan", "packaging", "piece", 5),
]

# key, label, value, help
STRAP_GLOBAL_SETTINGS = [
    ("strap_fob_cost_per_container_usd", "PET/PP Strap: FOB cost per container ($)", 1100,
     "Flat FOB handling cost per shipping container, shared by the PET Strap and PP Strap lines. "
     "Divided across the rolls that fit in a container (by core weight, 20ft/40ft, box/pallet) to "
     "get each line's per-roll FOB share -- see strap_pricing.py."),
    ("strap_shipping_rate_per_container_usd", "PET/PP Strap: shipping rate per container ($)", 1200,
     "Flat sea-freight cost per shipping container, shared by the PET Strap and PP Strap lines, "
     "added on top of the FOB share the same way to get each line's per-roll CFR price."),
    ("strap_credit_surcharge_usd_kg", "PET/PP Strap: credit-term surcharge ($/kg)", 0.03,
     "Added to the Cash FOB/CFR $/kg price when the quotation's payment term is not Cash."),
]

# v34 -- BOM recipes, seeded from the exact figures verified against the
# owner's own PET/PP Export Excel sheets (see strap_pricing.py's old BOM_DEFS,
# now moved here so they're admin-editable). Component keys are structural
# (map to a material_rate suffix + unit/currency in strap_pricing.LINE_CONFIG)
# and are not part of what's editable here -- only their fractions, plus each
# recipe's profit % and waste %, are.
# line_key, bom_key, profit_pct, waste_pct, components{component_key: fraction}
STRAP_BOM_SEED = [
    ("pet", "pet_green", 0.16, 0.01, {"resin": 0.96, "c4": 0.02, "color": 0.02}),
    ("pet", "pet_colors", 0.16, 0.01, {"resin": 0.935, "c4": 0.02, "color": 0.045}),
    ("pp", "pure_white", 0.12, 0.04, {"5032": 0.97, "coco3": 0.03}),
    ("pp", "pure_color", 0.12, 0.08, {"5032": 0.955, "color": 0.045}),
    ("pp", "recycled_pure_white", 0.16, 0.04, {"5032": 0.5, "recycled_pure": 0.45, "coco3": 0.05}),
    ("pp", "recycled_color", 0.20, 0.08, {"recycled_colored": 0.99, "color": 0.01}),
    ("pp", "recycled_pure_colors", 0.16, 0.08, {"5032": 0.5, "recycled_pure": 0.45, "color": 0.05}),
]

# v34 -- per-line electricity/fixed-cost/direct-labor figures, moved here
# from strap_pricing.LINE_CONFIG for the same reason (admin-editable).
# line_key, electricity_per_ton_egp, fixed_cost_per_kg_usd, direct_labor_per_kg_usd
STRAP_LINE_CONFIG_SEED = [
    ("pet", 3233.8378874999994, 0.15238135851623189, 0.0),
    ("pp", 4937.625783806608, 0.12505020582355653, 0.023584587962962967),
]

# line_key, code, bom_key, width_mm, thickness_mm, meters_per_coil, core_weight_kg, has_box, has_pallet, ctr20, ctr40
STRAP_PRODUCTS = [
    # -- PET (BOM: pet_green for all current catalog rows) --
    ("pet", "PET 12 x 0.531", "pet_green", 12, 0.531, 2800, 1, 0, 1, 0, 1),
    ("pet", "PET 12 x 0.582", "pet_green", 12, 0.582, 2500, 1, 0, 1, 0, 1),
    ("pet", "PET 12 x 0.678", "pet_green", 12, 0.678, 2000, 1, 0, 1, 0, 1),
    ("pet", "PET 15 x 0.72", "pet_green", 15, 0.72, 1400, 1, 0, 1, 0, 1),
    ("pet", "PET 15.5 x 0.823", "pet_green", 15.5, 0.823, 1200, 1, 0, 1, 0, 1),
    ("pet", "PET 16 x 1.03", "pet_green", 16, 1.03, 1000, 1, 0, 1, 0, 1),
    ("pet", "PET 19 x 1.026", "pet_green", 19, 1.026, 850, 1, 0, 1, 0, 1),
    ("pet", "PET 19 x 0.716", "pet_green", 19, 0.716, 1000, 1, 0, 1, 0, 1),
    ("pet", "PET 15 x 0.62 (1750m)", "pet_green", 15, 0.62, 1750, 1, 0, 1, 1, 0),
    ("pet", "PET 15 x 0.593", "pet_green", 15, 0.593, 2000, 1, 0, 1, 1, 0),
    ("pet", "PET 15 x 0.688", "pet_green", 15, 0.688, 1750, 1, 0, 1, 1, 0),
    ("pet", "PET 15 x 0.69", "pet_green", 15, 0.69, 1750, 1, 1, 1, 1, 0),
    ("pet", "PET 12 x 0.732", "pet_green", 12, 0.732, 2000, 1, 0, 1, 1, 0),
    ("pet", "PET 15 x 1", "pet_green", 15, 1, 1000, 1, 0, 1, 1, 1),
    ("pet", "PET 14 x 0.71", "pet_green", 14, 0.71, 1750, 1, 0, 1, 0, 1),
    ("pet", "PET 15 x 0.62 (750m)", "pet_green", 15, 0.62, 750, 1, 0, 1, 0, 1),
    ("pet", "PET 11.5 x 0.565", "pet_green", 11.5, 0.565, 2500, 1, 0, 1, 0, 1),
    # -- PP --
    ("pp", "PP 12071.3", "pure_white", 12, 0.713, 2000, 1, 1, 1, 0, 1),
    ("pp", "PP 12085", "pure_color", 12, 0.85, 2000, 1, 0, 1, 0, 1),
    ("pp", "PP 8064.1", "pure_white", 8, 0.641, 2700, 0.5, 1, 1, 0, 1),
    ("pp", "PP 12076", "pure_color", 12, 0.76, 1200, 0.25, 1, 1, 0, 1),
    ("pp", "PP 12071.4", "pure_white", 12, 0.714, 1500, 0.5, 1, 1, 0, 1),
]


def _seed_strap_data(conn):
    """v30 -- PET Strap / PP Strap: material rates, shared freight settings,
    and the starting catalog. Per-row idempotent (matches every other seed
    function in this file) so re-running on an already-seeded live DB adds
    nothing and never overwrites a value the owner has since edited."""
    for key, label, category, unit, value in STRAP_MATERIAL_RATES:
        exists = conn.execute("SELECT id FROM material_rate WHERE material_key=?", (key,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO material_rate (material_key, label, category, unit, value) VALUES (?,?,?,?,?)",
            (key, label, category, unit, value),
        )
    conn.commit()

    for key, label, value, help_text in STRAP_GLOBAL_SETTINGS:
        exists = conn.execute("SELECT key FROM global_setting WHERE key=?", (key,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO global_setting (key, label, value, help) VALUES (?,?,?,?)",
            (key, label, value, help_text),
        )
    conn.commit()

    # v34 -- BOM recipes (profit/waste/composition %) and per-line fixed
    # inputs (electricity/fixed cost/direct labor), per-row idempotent like
    # everything else here so an owner's own edits on an already-seeded live
    # DB are never overwritten by a later boot.
    for line_key, bom_key, profit_pct, waste_pct, components in STRAP_BOM_SEED:
        exists = conn.execute(
            "SELECT id FROM strap_bom WHERE line_key=? AND bom_key=?", (line_key, bom_key)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO strap_bom (line_key, bom_key, profit_pct, waste_pct, components_json)
               VALUES (?,?,?,?,?)""",
            (line_key, bom_key, profit_pct, waste_pct, json.dumps(components)),
        )
    conn.commit()

    for line_key, electricity, fixed_cost, direct_labor in STRAP_LINE_CONFIG_SEED:
        exists = conn.execute(
            "SELECT line_key FROM strap_line_config WHERE line_key=?", (line_key,)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO strap_line_config
               (line_key, electricity_per_ton_egp, fixed_cost_per_kg_usd, direct_labor_per_kg_usd)
               VALUES (?,?,?,?)""",
            (line_key, electricity, fixed_cost, direct_labor),
        )
    conn.commit()

    for (line_key, code, bom_key, width_mm, thickness_mm, meters_per_coil, core_weight_kg,
         has_box, has_pallet, ctr20, ctr40) in STRAP_PRODUCTS:
        exists = conn.execute(
            "SELECT id FROM strap_product WHERE line_key=? AND code=?", (line_key, code)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO strap_product
               (line_key, code, bom_key, width_mm, thickness_mm, meters_per_coil, core_weight_kg,
                has_box, has_pallet, ctr20, ctr40)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (line_key, code, bom_key, width_mm, thickness_mm, meters_per_coil, core_weight_kg,
             has_box, has_pallet, ctr20, ctr40),
        )
    conn.commit()
