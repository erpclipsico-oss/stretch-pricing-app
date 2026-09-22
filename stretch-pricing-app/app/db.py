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
