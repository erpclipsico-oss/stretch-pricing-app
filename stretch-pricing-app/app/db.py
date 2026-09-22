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
    price_adjustment_usd_kg REAL NOT NULL DEFAULT 0
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
    FOREIGN KEY (quotation_id) REFERENCES quotation(id),
    FOREIGN KEY (product_id) REFERENCES product(id)
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
    conn.close()


def _migrate(conn):
    """Add columns that didn't exist in earlier deployments, so an existing
    live database (already seeded) picks up new fields without a reset."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(user)").fetchall()}
    if "price_adjustment_usd_kg" not in cols:
        conn.execute("ALTER TABLE user ADD COLUMN price_adjustment_usd_kg REAL NOT NULL DEFAULT 0")
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


def _seed_default_users(conn):
    existing = conn.execute("SELECT COUNT(*) c FROM user").fetchone()["c"]
    if existing:
        return
    conn.execute(
        "INSERT INTO user (username, full_name, password_hash, role, region) VALUES (?,?,?,?,?)",
        ("admin", "Administrator", generate_password_hash("ChangeMe123!"), "admin", "Egypt"),
    )
    conn.execute(
        "INSERT INTO user (username, full_name, password_hash, role, region) VALUES (?,?,?,?,?)",
        ("sales_rep1", "Sales Rep", generate_password_hash("ChangeMe123!"), "sales_rep", "Egypt"),
    )
    conn.commit()
