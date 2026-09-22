"""Generic per-table Excel round-trip: download a cost table as a fillable
.xlsx, upload it back, review a before/after diff (including the estimated
effect on the final sales price) and only then apply it to the live DB.

This is the lighter-weight, single-table analog of cost_upload.py's
multi-sheet annual-upload review-then-apply flow -- same idea (never commit
until the user explicitly confirms), reused rather than duplicated where it
makes sense (cost_engine.recalculate_all_products / sync_labor_to_fixed_costs
are called exactly as the rest of the app already calls them).

Rows are always matched back to their DB row by the *_id* column exported in
column A of the workbook -- never by row position -- so inserting/reordering
rows in Excel can never silently mismatch a value to the wrong DB row (see
cost_upload.py's own comments for the earlier bug this guards against).
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .db import DB_PATH
from . import cost_engine
from . import pricing


# ---------------------------------------------------------------------
# Table registry. (db_column, excel_header, editable) per table; id_col is
# the column re-upload matches rows back to (never row position).
# ---------------------------------------------------------------------

TABLE_CONFIGS = {
    "global_setting": {
        "label": "Global Cost Settings",
        "table": "global_setting",
        "id_col": "key",
        "id_type": "text",
        "order_by": "label",
        "label_template": "{label}",
        "redirect_endpoint": "admin_global_settings",
        "columns": [
            ("label", "Setting", False),
            ("help", "Description", False),
            ("value", "Value", True),
        ],
    },
    "material_rate": {
        "label": "Material Rates",
        "table": "material_rate",
        "id_col": "id",
        "order_by": "category, label",
        "label_template": "{label}",
        "redirect_endpoint": "admin_material_rates",
        "columns": [
            ("label", "Material", False),
            ("category", "Category", False),
            ("unit", "Unit", False),
            ("value", "Rate", True),
        ],
    },
    "labor_employee": {
        "label": "Labor Roster",
        "table": "labor_employee",
        "id_col": "id",
        "order_by": "active DESC, name",
        "label_template": "{name}",
        "redirect_endpoint": "admin_labor",
        "after_apply": "sync_labor",
        "columns": [
            ("name", "Employee Name", False),
            ("role", "Role", False),
            ("base_2023_egp", "Base Wage 2023 (EGP)", True),
            ("increase_rate", "Increase Rate (0.3 = 30%)", True),
        ],
    },
    "electricity_power": {
        "label": "Electricity - Machine Power (kW/ton)",
        "table": "electricity_power",
        "id_col": "id",
        "order_by": "roll_type, micron",
        "label_template": "{roll_type} {micron}µm",
        "redirect_endpoint": "admin_electricity",
        "columns": [
            ("micron", "Micron", False),
            ("roll_type", "Roll Type", False),
            ("kw_per_ton", "kW per ton", True),
        ],
    },
    "production_capacity": {
        "label": "Electricity - Production Capacity (tons/day)",
        "table": "production_capacity",
        "id_col": "id",
        "order_by": "roll_type, micron",
        "label_template": "{roll_type} {micron}µm",
        "redirect_endpoint": "admin_electricity",
        "columns": [
            ("micron", "Micron", False),
            ("roll_type", "Roll Type", False),
            ("tons_per_day", "Tons per day", True),
        ],
    },
    "variable_cost_item": {
        "label": "Variable Costs",
        "table": "variable_cost_item",
        "id_col": "id",
        "order_by": "id",
        "label_template": "{name}",
        "redirect_endpoint": "admin_variable_costs",
        "columns": [
            ("name", "Item", False),
            ("value_egp_per_ton", "EGP per ton", True),
        ],
    },
    "fixed_cost_item": {
        "label": "Fixed Costs",
        "table": "fixed_cost_item",
        "id_col": "id",
        "order_by": "category, id",
        "label_template": "{category}: {name}",
        "redirect_endpoint": "admin_fixed_costs",
        "columns": [
            ("category", "Category", False),
            ("name", "Item", False),
            ("value_egp", "EGP", True),
        ],
    },
    "pallet_component": {
        "label": "Pallet / Packaging Component",
        "table": "pallet_component",
        "id_col": "id",
        "order_by": "label",
        "label_template": "{label}",
        "redirect_endpoint": "admin_pallet",
        "columns": [
            ("label", "Packing Type", False),
            ("pallet_qty", "Pallet Qty", True),
            ("cardboard_qty", "Cardboard Qty", True),
            ("cap_qty", "Cap Qty", True),
            ("corrugated_kg", "Corrugated (kg)", True),
            ("stretch_kg", "Stretch (kg)", True),
            ("box_qty", "Box Qty", True),
            ("rolls_per_box", "Rolls/Box", True),
            ("cartoon_angle_qty", "Cartoon Angle Qty", True),
            ("scotch_tape_qty", "Scotch Tape Qty", True),
            ("air_bag_qty", "Air Bags Qty", True),
            ("pe_bag_qty", "PE Bag Qty (kg)", True),
        ],
    },
    "bom_row": {
        "label": "Bill of Materials (BOM)",
        "table": "bom_row",
        "id_col": "id",
        "order_by": "stretch_multiplier, roll_tier, micron",
        "label_template": "{stretch_multiplier}x / {micron}µm / {roll_tier}",
        "redirect_endpoint": "admin_bom",
        "columns": [
            ("stretch_multiplier", "Multiplier", False),
            ("micron", "Micron", False),
            ("roll_tier", "Roll Tier", False),
            ("exceed3518", "Exceed3518", True),
            ("exceed3812", "Exceed3812", True),
            ("exceedxp", "ExceedXP", True),
            ("vista6000", "Vista6000", True),
            ("enable", "Enable", True),
            ("ld258", "LD258", True),
            ("vista6202", "Vista6202", True),
        ],
    },
    "packing_tier": {
        "label": "Packing Tiers",
        "table": "packing_tier",
        "id_col": "id",
        "order_by": "category, pallet_type, match_weight_kg",
        "label_template": "{weight_label} / {pallet_type}",
        "redirect_endpoint": "admin_packing_tiers",
        "columns": [
            ("category", "Category", False),
            ("weight_label", "Weight Bucket", False),
            ("pallet_type", "Pallet Type", False),
            ("match_weight_kg", "Match Weight (kg)", True),
            ("rolls_per_box", "Rolls/Box", True),
            ("box_per_pallet", "Box/Pallet", True),
            ("rolls_per_pallet", "Rolls/Pallet", True),
            ("pallets_per_container40", "Pallets/Container 40'", True),
            ("pallets_per_container20", "Pallets/Container 20'", True),
        ],
    },
}


# ---------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------

def export_table_excel(conn, table_key):
    """Returns a BytesIO of a fillable .xlsx: column A is the DB row id
    (grey, "do not edit" -- used to match rows back on re-upload, never row
    position), read-only reference columns are grey, editable columns are
    highlighted yellow."""
    import io

    cfg = TABLE_CONFIGS[table_key]
    rows = conn.execute(f"SELECT * FROM {cfg['table']} ORDER BY {cfg['order_by']}").fetchall()

    wb = Workbook()
    ws = wb.active
    # Excel sheet titles forbid \ / ? * [ ] : and are capped at 31 chars --
    # strip those out rather than let a label like "Pallet / Packaging
    # Component" crash the export.
    safe_title = re.sub(r"[\\/?*\[\]:]", "", cfg["label"])[:31].strip()
    ws.title = safe_title or "Sheet1"

    headers = ["Row ID (keep)"] + [h for _, h, _ in cfg["columns"]]
    ws.append(headers)
    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font

    for row in rows:
        ws.append([row[cfg["id_col"]]] + [row[c] for c, _, _ in cfg["columns"]])

    id_fill = PatternFill(start_color="E5E7EB", end_color="E5E7EB", fill_type="solid")
    readonly_fill = PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid")
    editable_fill = PatternFill(start_color="FEF9C3", end_color="FEF9C3", fill_type="solid")
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=1).fill = id_fill
        for i, (_, _, editable) in enumerate(cfg["columns"], start=2):
            ws.cell(row=r, column=i).fill = editable_fill if editable else readonly_fill

    ws.column_dimensions["A"].width = 10
    for i in range(2, len(cfg["columns"]) + 2):
        ws.column_dimensions[get_column_letter(i)].width = 20
    ws.freeze_panes = "B2"

    note_row = ws.max_row + 2
    ws.cell(row=note_row, column=1,
            value="Yellow cells = fill these in. Grey cells = reference only, do not edit. "
                  "Do not edit, delete or reorder the 'Row ID' column.")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------

def parse_uploaded_excel(file_stream, table_key):
    """Returns a list of {"id": <row id>, <editable_db_col>: <new float value>, ...}
    -- one dict per row that has at least one usable editable value. Blank
    cells, blank rows and non-numeric junk are skipped rather than raising,
    so a partially-filled or messily-edited sheet never crashes the upload."""
    cfg = TABLE_CONFIGS[table_key]
    wb = openpyxl.load_workbook(file_stream, data_only=True)
    ws = wb.active

    header_row = None
    for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
        header_row = row
    if not header_row:
        return []

    idx_by_header = {}
    for i, h in enumerate(header_row):
        if isinstance(h, str):
            idx_by_header[h.strip().lower()] = i
    id_idx = idx_by_header.get("row id (keep)", 0)

    col_idx_map = {}
    for col, hdr, editable in cfg["columns"]:
        if not editable:
            continue
        idx = idx_by_header.get(hdr.strip().lower())
        if idx is not None:
            col_idx_map[col] = idx

    results = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row is None or all(v is None for v in row):
            continue
        if id_idx >= len(row):
            continue
        id_val = row[id_idx]
        if id_val is None or (isinstance(id_val, str) and id_val.strip() == ""):
            continue
        rec = {"id": id_val}
        has_field = False
        for col, idx in col_idx_map.items():
            if idx >= len(row):
                continue
            val = row[idx]
            if val is None or (isinstance(val, str) and val.strip() == ""):
                continue
            try:
                rec[col] = float(val)
                has_field = True
            except (TypeError, ValueError):
                continue
        if has_field:
            results.append(rec)
    return results


# ---------------------------------------------------------------------
# Diff / apply
# ---------------------------------------------------------------------

def _norm_id(val, id_type):
    if val is None:
        return None
    if id_type == "text":
        return str(val).strip()
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _category_bucket(cat):
    if not cat:
        return "Other"
    if "power_plus" in cat:
        return "Power+"
    if "power" in cat:
        return "Power"
    if cat == "uv_regid":
        return "UV/Regid"
    if cat == "regid":
        return "Regid"
    if "standard" in cat:
        return "Standard"
    return cat


def _snapshot_prices(conn):
    """Live final-sales-price ($/KG, at a representative Moderate/Class-A/
    Standard-roll, zero-adjustment quote) for every ordinary (non
    Pre-Stretch, made-to-order) product, using the real pricing.unit_price_for()
    -- the same function the quote builder itself calls -- so the % impact
    reported is the actual final-price effect, not just the raw ex-work cost's
    own % change (margin is applied additively via price_adjustment as well
    as multiplicatively via the factor, so the two are not always identical)."""
    rows = conn.execute("SELECT * FROM product WHERE COALESCE(is_prestretch,0)=0").fetchall()
    out = {}
    for p in rows:
        try:
            up = pricing.unit_price_for(conn, p, "Moderate", "A", "standard", 0)
        except Exception:
            up = None
        out[p["id"]] = {
            "unit_price": up,
            "label": pricing.product_label(p),
            "category": _category_bucket(pricing.product_category(p)),
        }
    return out


def _summarize_price_impact(before, after):
    changed = []
    for pid, b in before.items():
        a = after.get(pid)
        if not a or a["unit_price"] is None or b["unit_price"] in (None, 0):
            continue
        pct = (a["unit_price"] - b["unit_price"]) / b["unit_price"] * 100
        if abs(pct) > 1e-6:
            changed.append({
                "product_id": pid, "label": b["label"], "category": b["category"],
                "before": round(b["unit_price"], 4), "after": round(a["unit_price"], 4),
                "pct": round(pct, 3),
            })
    if not changed:
        return {"count": 0, "products_evaluated": len(before)}

    pcts = [c["pct"] for c in changed]
    cat_groups = {}
    for c in changed:
        cat_groups.setdefault(c["category"], []).append(c["pct"])
    category_breakdown = [
        {"category": k, "count": len(v), "avg_pct": round(sum(v) / len(v), 3),
         "min_pct": round(min(v), 3), "max_pct": round(max(v), 3)}
        for k, v in sorted(cat_groups.items())
    ]
    top_products = sorted(changed, key=lambda c: abs(c["pct"]), reverse=True)[:5]
    return {
        "count": len(changed),
        "products_evaluated": len(before),
        "min_pct": round(min(pcts), 3),
        "max_pct": round(max(pcts), 3),
        "avg_pct": round(sum(pcts) / len(pcts), 3),
        "category_breakdown": category_breakdown,
        "top_products": top_products,
    }


def _apply_updates_and_measure(conn, table_key, parsed_rows):
    """Writes the proposed values into `conn` (does NOT commit or roll
    back -- caller decides), returns (field_diffs, price_impact). Also
    triggers the same recalculation hooks the hand-edit admin routes
    already call (sync_labor_to_fixed_costs / recalculate_all_products),
    so the price snapshot taken afterwards reflects the full propagation."""
    cfg = TABLE_CONFIGS[table_key]
    table = cfg["table"]
    id_col = cfg["id_col"]
    id_type = cfg.get("id_type", "int")
    header_map = {c: h for c, h, _ in cfg["columns"]}
    label_template = cfg["label_template"]

    current = {}
    for row in conn.execute(f"SELECT * FROM {table}").fetchall():
        current[_norm_id(row[id_col], id_type)] = row

    before_prices = _snapshot_prices(conn)

    field_diffs = []
    for pr in parsed_rows:
        rid = _norm_id(pr.get("id"), id_type)
        row = current.get(rid)
        if row is None:
            continue
        try:
            row_label = label_template.format(**{k: row[k] for k in row.keys()})
        except Exception:
            row_label = str(rid)
        updates = {}
        for col, hdr, editable in cfg["columns"]:
            if not editable or col not in pr:
                continue
            new_val = pr[col]
            old_raw = row[col]
            old_val = old_raw if old_raw is not None else 0.0
            try:
                old_f = float(old_val)
                new_f = float(new_val)
            except (TypeError, ValueError):
                continue
            if abs(new_f - old_f) > 1e-9:
                pct = ((new_f - old_f) / old_f * 100) if old_f else (100.0 if new_f else 0.0)
                field_diffs.append({
                    "row_label": row_label, "field": header_map[col],
                    "old": round(old_f, 4), "new": round(new_f, 4), "pct_change": round(pct, 2),
                })
                updates[col] = new_f
        if updates:
            set_clause = ", ".join(f"{c}=?" for c in updates)
            params = list(updates.values()) + [rid]
            conn.execute(f"UPDATE {table} SET {set_clause} WHERE {id_col}=?", params)

    if field_diffs:
        if cfg.get("after_apply") == "sync_labor":
            cost_engine.sync_labor_to_fixed_costs(conn)
        cost_engine.recalculate_all_products(conn)

    after_prices = _snapshot_prices(conn)
    price_impact = _summarize_price_impact(before_prices, after_prices)
    return field_diffs, price_impact


def _open_temp_copy():
    """Opens a throwaway copy of the live SQLite file for the preview step,
    so the preview can safely call helper functions that commit internally
    (cost_engine.recalculate_all_products / sync_labor_to_fixed_costs)
    without ever risking a partial write reaching the real, live database --
    a plain rollback() on the shared connection would not be safe here since
    those helpers call conn.commit() themselves."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy2(DB_PATH, tmp_path)
    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, tmp_path


def diff_table(conn, table_key, parsed_rows):
    """Preview only -- the live `conn` is never written to. Returns
    {"field_diffs": [...], "price_impact": {...}}."""
    if table_key not in TABLE_CONFIGS:
        raise ValueError(f"Unknown table_key: {table_key}")
    tmp_conn, tmp_path = _open_temp_copy()
    try:
        field_diffs, price_impact = _apply_updates_and_measure(tmp_conn, table_key, parsed_rows)
    finally:
        tmp_conn.close()
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return {"field_diffs": field_diffs, "price_impact": price_impact}


def apply_table_changes(conn, table_key, parsed_rows, uploaded_by, filename):
    """Writes the confirmed changes to the live `conn`, recalculates, logs
    to table_upload_log, and commits once at the end. Returns
    (field_diffs, price_impact)."""
    if table_key not in TABLE_CONFIGS:
        raise ValueError(f"Unknown table_key: {table_key}")
    field_diffs, price_impact = _apply_updates_and_measure(conn, table_key, parsed_rows)
    summary = {
        "field_diffs": field_diffs,
        "price_impact": {k: v for k, v in price_impact.items() if k != "top_products"},
    }
    conn.execute(
        "INSERT INTO table_upload_log (table_key, uploaded_at, uploaded_by, filename, summary_json) "
        "VALUES (?,?,?,?,?)",
        (table_key, datetime.now(timezone.utc).isoformat(), uploaded_by, filename,
         json.dumps(summary, ensure_ascii=False)),
    )
    conn.commit()
    return field_diffs, price_impact


def get_last_upload(conn, table_key):
    return conn.execute(
        "SELECT * FROM table_upload_log WHERE table_key=? ORDER BY uploaded_at DESC LIMIT 1", (table_key,)
    ).fetchone()
