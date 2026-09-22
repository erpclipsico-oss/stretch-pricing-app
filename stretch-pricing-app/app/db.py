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
    seller_type TEXT NOT NULL DEFAULT 'local'
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
    total_kg REAL DEFAULT 0,
    line_discount_pct REAL DEFAULT 0,
    pricing_basis TEXT NOT NULL DEFAULT 'per_kg',
    FOREIGN KEY (quotation_id) REFERENCES quotation(id),
    FOREIGN KEY (product_id) REFERENCES product(id)
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
    _seed_freight_v2(conn)
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

    # Seed the two Egyptian loading ports with their FOB add-on (idempotent,
    # keyed by the unique port name, so it won't duplicate or clobber a rate
    # the owner has since edited in admin).
    conn.execute("INSERT OR IGNORE INTO loading_port (port, fob_addon_usd) VALUES ('Alexandria (Egypt)', 1500)")
    conn.execute("INSERT OR IGNORE INTO loading_port (port, fob_addon_usd) VALUES ('Damietta (Egypt)', 1800)")
    conn.commit()


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


def _seed_default_users(conn):
    """Idempotent per-username seeding (not gated on the whole table being
    empty) so new default accounts (e.g. pasquale/manuel, added later) get
    created on an already-deployed, already-seeded live DB too, without
    touching or duplicating any existing account."""
    for username, full_name, role, region, seller_type, adjustment in DEFAULT_USERS:
        exists = conn.execute("SELECT id FROM user WHERE username=?", (username,)).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO user (username, full_name, password_hash, role, region,
                                  seller_type, price_adjustment_usd_kg)
               VALUES (?,?,?,?,?,?,?)""",
            (username, full_name, generate_password_hash("ChangeMe123!"), role, region,
             seller_type, adjustment),
        )
    conn.commit()
