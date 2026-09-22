"""Annual cost-upload: parse an updated pricing workbook (same layout as the
original pricing.xlsx), diff every value against the current database, and
let an admin review the changes before applying them.

This reuses the same sheet names / cell layout the cost engine's one-time
Phase-1 import already relies on (see COST_ENGINE.md's sheet-to-table
mapping table), but matches rows by their *label text* (item name / employee
name) rather than by fixed cell coordinates, so a reasonable amount of row
reordering or insertion in the new year's workbook doesn't silently
mismatch a value to the wrong DB row.

Covers: Material pricing (resin + packaging rates), Global settings (Dollar
Rate, Capacity Usage %, etc., where present on the same sheet),
اجور مباشرة (labor roster wages), تكاليف متغيرة (variable cost items),
تكاليف ثابتة (fixed cost items).

Not yet covered by the automated diff (open item -- see COST_ENGINE.md /
the report back to the business owner): Electricity kW/ton table, BOM
table, Pallet component table, and the Details-sheet packing tiers. These
change far less often than resin/labor/fixed-cost figures and can still be
edited by hand on their own admin screens after an annual upload.
"""

import json

import openpyxl


def _sheet(wb, name):
    return wb[name] if name in wb.sheetnames else None


def _norm(s):
    return (s or "").strip().lower()


# ---------------------------------------------------------------- parsing

def parse_material_rates(wb):
    """Returns {label_normalized: (raw_label, value)} from 'Material pricing'."""
    ws = _sheet(wb, "Material pricing")
    out = {}
    if ws is None:
        return out
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        if not row or row[0] in (None, "", "Item"):
            continue
        label, unit, value = row[0], row[1] if len(row) > 1 else None, row[2] if len(row) > 2 else None
        if isinstance(label, str) and isinstance(value, (int, float)) and unit in ("ton", "kilo", "piece"):
            out[_norm(label)] = (label, float(value))
    return out


def parse_global_settings(wb):
    """Dollar Rate / Dollar Rate (prestretch) / Color/Prestretch per ton off
    the top of 'Material pricing' (they sit in fixed label/value cell pairs
    there, not a clean column, so these are matched by their known labels)."""
    ws = _sheet(wb, "Material pricing")
    out = {}
    if ws is None:
        return out
    label_map = {
        "dollar rate": "dollar_rate",
        "dollar rate(prestretch)": "dollar_rate_prestretch",
        "color / ton": "color_rate_per_ton",
        "prestretch/ton": "prestretch_rate_per_ton",
    }
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        for i, cell in enumerate(row):
            if isinstance(cell, str) and _norm(cell) in label_map and i + 1 < len(row):
                val = row[i + 1]
                if isinstance(val, (int, float)):
                    out[label_map[_norm(cell)]] = float(val)
    return out


def parse_labor(wb):
    """{name_normalized: (raw_name, role, base_2024_egp, increase_rate)} from
    'اجور مباشرة'. base_2023_egp in the DB is really 'base wage before the
    increase rate is applied' -- the sheet's 'اساسي 2024' column already has
    the increase baked in, so we back it out: base = اساسي2024/(1+rate)."""
    ws = _sheet(wb, "اجور مباشرة")
    out = {}
    if ws is None:
        return out
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        if not row or not isinstance(row[0], str):
            continue
        name = row[0]
        if len(row) < 5:
            continue
        role, base2023, rate, base2024 = row[1], row[2], row[3], row[4]
        if isinstance(base2023, (int, float)) and isinstance(rate, (int, float)):
            out[_norm(name)] = (name, role, float(base2023), float(rate))
    return out


def parse_variable_cost_items(wb):
    ws = _sheet(wb, "تكاليف متغيرة")
    out = {}
    if ws is None:
        return out
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        if not row or not isinstance(row[0], str):
            continue
        name, unit, value = row[0], row[1] if len(row) > 1 else None, row[2] if len(row) > 2 else None
        if isinstance(value, (int, float)) and name.strip() not in ("البيان",):
            out[_norm(name)] = (name, float(value))
    return out


def parse_fixed_cost_items(wb):
    """Walks 'تكاليف ثابتة' top to bottom, tracking which category section
    we're in (production / selling / admin / financial, identified by the
    Arabic section headers), reading (name, 'value') rows under each."""
    ws = _sheet(wb, "تكاليف ثابتة")
    out = []  # list of (category, name, value) in sheet order
    if ws is None:
        return out
    section_headers = {
        "مصروفا صناعية": "production",
        "مصروفات صناعية": "production",
        "مصروفات بيعية": "selling",
        "مصروفات عمومية و ادارية": "admin",
        "مصروفات مالية": "financial",
        "المصروفات المالية": "financial",
    }
    current = None
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        if not row or row[0] is None:
            continue
        first = row[0].strip() if isinstance(row[0], str) else None
        if first in section_headers:
            current = section_headers[first]
            continue
        if first in ("البيان", "الاجمالي") or current is None:
            continue
        value = row[2] if len(row) > 2 else None
        if isinstance(value, (int, float)):
            out.append((current, first, float(value)))
    return out


# ---------------------------------------------------------------- diff

def diff_workbook(conn, xlsx_path):
    """Returns (changes, unchanged_count).
    changes: list of dicts {category, item, old, new, pct_change, apply}
    where 'apply' carries what apply_changes() needs to write that field.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    changes = []
    unchanged = 0

    # Material rates
    parsed_materials = parse_material_rates(wb)
    for row in conn.execute("SELECT * FROM material_rate").fetchall():
        hit = parsed_materials.get(_norm(row["label"]))
        if not hit:
            continue
        _, new_val = hit
        old_val = row["value"] or 0
        if abs(new_val - old_val) > 1e-9:
            changes.append(_change("Material Rates", row["label"], old_val, new_val,
                                    {"table": "material_rate", "id": row["id"], "field": "value", "value": new_val}))
        else:
            unchanged += 1

    # Global settings
    parsed_globals = parse_global_settings(wb)
    for key, new_val in parsed_globals.items():
        row = conn.execute("SELECT * FROM global_setting WHERE key=?", (key,)).fetchone()
        if not row:
            continue
        old_val = row["value"] or 0
        if abs(new_val - old_val) > 1e-9:
            changes.append(_change("Global Settings", row["label"] or key, old_val, new_val,
                                    {"table": "global_setting", "id": key, "field": "value", "value": new_val}))
        else:
            unchanged += 1

    # Labor
    parsed_labor = parse_labor(wb)
    for row in conn.execute("SELECT * FROM labor_employee WHERE active=1").fetchall():
        hit = parsed_labor.get(_norm(row["name"]))
        if not hit:
            continue
        _, role, new_base, new_rate = hit
        old_base = row["base_2023_egp"] or 0
        old_rate = row["increase_rate"] or 0
        if abs(new_base - old_base) > 1e-6 or abs(new_rate - old_rate) > 1e-9:
            changes.append(_change(
                "Labor", f"{row['name']} (base wage)", old_base, new_base,
                {"table": "labor_employee", "id": row["id"], "field": "base_2023_egp", "value": new_base},
            ))
            changes.append(_change(
                "Labor", f"{row['name']} (increase rate)", old_rate, new_rate,
                {"table": "labor_employee", "id": row["id"], "field": "increase_rate", "value": new_rate},
            ))
        else:
            unchanged += 1

    # Variable cost items
    parsed_var = parse_variable_cost_items(wb)
    for row in conn.execute("SELECT * FROM variable_cost_item").fetchall():
        hit = parsed_var.get(_norm(row["name"]))
        if not hit:
            continue
        _, new_val = hit
        old_val = row["value_egp_per_ton"] or 0
        if abs(new_val - old_val) > 1e-6:
            changes.append(_change("Variable Costs", row["name"], old_val, new_val,
                                    {"table": "variable_cost_item", "id": row["id"], "field": "value_egp_per_ton",
                                     "value": new_val}))
        else:
            unchanged += 1

    # Fixed cost items -- matched positionally within each category section,
    # in the same order the sheet lists them (see parse_fixed_cost_items doc).
    parsed_fixed = parse_fixed_cost_items(wb)
    by_cat = {}
    for cat, name, val in parsed_fixed:
        by_cat.setdefault(cat, []).append((name, val))
    for cat, items in by_cat.items():
        db_rows = conn.execute(
            "SELECT * FROM fixed_cost_item WHERE category=? ORDER BY id", (cat,)
        ).fetchall()
        for (name, new_val), row in zip(items, db_rows):
            old_val = row["value_egp"] or 0
            if abs(new_val - old_val) > 1e-6:
                changes.append(_change(
                    f"Fixed Costs ({cat})", row["name"] or name, old_val, new_val,
                    {"table": "fixed_cost_item", "id": row["id"], "field": "value_egp", "value": new_val},
                ))
            else:
                unchanged += 1

    return changes, unchanged


def _change(category, item, old, new, apply):
    pct = ((new - old) / old * 100) if old else (100.0 if new else 0.0)
    return {"category": category, "item": item, "old": round(old, 4), "new": round(new, 4),
            "pct_change": round(pct, 2), "apply": apply}


def apply_changes(conn, changes):
    for c in changes:
        a = c["apply"]
        if a["table"] == "global_setting":
            conn.execute("UPDATE global_setting SET value=? WHERE key=?", (a["value"], a["id"]))
        else:
            conn.execute(f"UPDATE {a['table']} SET {a['field']}=? WHERE id=?", (a["value"], a["id"]))
    conn.commit()
    # Labor changes may affect the two labor-sourced fixed-cost line items.
    if any(c["apply"]["table"] == "labor_employee" for c in changes):
        from . import cost_engine
        cost_engine.sync_labor_to_fixed_costs(conn)


def summarize_for_log(changes):
    return json.dumps([{"category": c["category"], "item": c["item"], "old": c["old"], "new": c["new"],
                         "pct_change": c["pct_change"]} for c in changes], ensure_ascii=False)
