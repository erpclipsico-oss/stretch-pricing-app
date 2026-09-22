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
    """Looks up a worksheet by name. `name` may be a single sheet name or a
    list of candidate names to try in order -- some years' workbooks rename
    a sheet tab (e.g. 'اجور مباشرة' -> 'Direct Labor', 'تكاليف متغيرة' ->
    'Variable', 'تكاليف ثابتة' -> 'Fixed'), so callers pass both the
    original Arabic tab name and any English variant seen in a later
    workbook, and whichever one exists in this file is used."""
    candidates = name if isinstance(name, (list, tuple)) else [name]
    for cand in candidates:
        if cand in wb.sheetnames:
            return wb[cand]
    return None


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


def parse_electricity(wb):
    """Returns (variable_tariff_egp_per_kwh, {(micron_float, roll_type): kw_per_ton})
    from the 'Electricity' sheet. Two independent grids share the sheet: a
    left block (St / P / P+ columns) and a right block (RIGID column),
    exactly like the Phase-1 seed data's layout -- read directly rather
    than assumed, since a future year could reorder the micron rows.
    roll_type keys use the same spelling the electricity_power table's
    'roll_type' column already uses ('St', 'P', 'P_plus', 'RIGID' -- note
    the underscore, not '+', matching a SQL-column-safe value), and micron
    is kept as a float (not the workbook's occasional int) so it compares
    equal to the DB's REAL column regardless of how each side was typed."""
    ws = _sheet(wb, ["Electricity"])
    tariff = None
    power = {}
    if ws is None:
        return tariff, power
    # The sheet has TWO grids that both start with a numeric micron in col0
    # and numeric values in cols 2/4/6/9/11 -- the kW/ton grid we want, and
    # a second "Variable Electricity Amount / Day" grid further down whose
    # columns are Ton/EGP-per-day, not kW/ton, but line up in the exact
    # same column positions. Only the first grid (immediately below the row
    # whose col2/col11 headers say 'KW/Ton') is what electricity_power
    # stores, so parsing stops the moment that specific grid ends (first
    # blank row after it started), never reading into the second grid.
    in_kw_grid = False
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        if not row:
            continue
        if isinstance(row[0], str) and _norm(row[0]) == "tariff" and isinstance(row[1], (int, float)):
            tariff = float(row[1])
        header_cells = [c for c in row if isinstance(c, str)]
        if any(_norm(c) == "kw/ton" for c in header_cells):
            in_kw_grid = True
            continue
        if in_kw_grid and row[0] is None:
            in_kw_grid = False  # blank row ends this grid
            continue
        if not in_kw_grid:
            continue
        # Left block: micron in col0, kw/ton for St/P/P+ in cols 2/4/6.
        if isinstance(row[0], (int, float)) and len(row) > 6:
            micron = float(row[0])
            for roll_type, idx in (("St", 2), ("P", 4), ("P_plus", 6)):
                val = row[idx]
                if isinstance(val, (int, float)):
                    power[(micron, roll_type)] = float(val)
        # Right block: micron in col9, kw/ton RIGID in col11.
        if len(row) > 11 and isinstance(row[9], (int, float)) and isinstance(row[11], (int, float)):
            power[(float(row[9]), "RIGID")] = float(row[11])
    return tariff, power


def parse_labor(wb):
    """{name_normalized: (raw_name, role, base_2024_egp, increase_rate)} from
    'اجور مباشرة'. base_2023_egp in the DB is really 'base wage before the
    increase rate is applied' -- the sheet's 'اساسي 2024' column already has
    the increase baked in, so we back it out: base = اساسي2024/(1+rate)."""
    ws = _sheet(wb, ["اجور مباشرة", "Direct Labor"])
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


VARIABLE_COST_LABEL_ALIASES = {
    "customs gratuity for explosives": "اكرامية مفرقعات",
    "guarantee letter recovery commission": "عمولة استرداد خطاب ضمان",
    "interest on raw materials payable": "فوائد مدينة خامات",
}


def parse_variable_cost_items(wb):
    ws = _sheet(wb, ["تكاليف متغيرة", "Variable"])
    out = {}
    if ws is None:
        return out
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        if not row or not isinstance(row[0], str):
            continue
        name, unit, value = row[0], row[1] if len(row) > 1 else None, row[2] if len(row) > 2 else None
        if isinstance(value, (int, float)) and name.strip() not in ("البيان", "Description"):
            canonical = VARIABLE_COST_LABEL_ALIASES.get(_norm(name), name)
            out[_norm(canonical)] = (name, float(value))
    return out


FIXED_COST_LABEL_ALIASES = {
    # English label (normalized) -> the Arabic label originally seeded into
    # fixed_cost_item.name, so a later workbook whose sheet tab and row
    # labels have been translated to English still matches the right DB
    # row *by name* rather than by row position. Position-based matching
    # broke in practice the first time a new line item ("Depreciation SML")
    # was inserted mid-section: everything after it silently paired with
    # the wrong neighbouring row. Matching by name (via this table) is
    # immune to insertions, deletions or reordering in a future workbook.
    "primo pack electrecity": "كهرباء بريموباك",
    "water": "مياه",
    "direct labor wages": "اجور عمال الانتاج",
    "direct labor overtime": "اضافي عمال الانتاج",
    "in direct labor salaries": "مرتبات صناعية غ.م.",
    "in direct labor overtime": "اضافي مرتبات صناعية غ.م.",
    "travel and transportation": "انتقالات وماموريات",
    "medical treatment": "علاج",
    "social insurance": "تأمينات اجتماعية",  # appears in Production, Selling and Admin, all under this same Arabic name
    "bonuses and incentives": "مكافأت",  # production section's label -- admin's own "مكافأت و حوافز" is a genuine collision (identical English text, different Arabic), resolved via CATEGORY_SCOPED_FIXED_COST_ALIASES below
    "holidays and occasions": "اعياد و مناسبات",
    'depreciation expense "general"': 'مصروف الاهلاك "عام"',
    'depreciation "primo pack"': 'مصروف الاهلاك "بريموباك"',
    'depreciation "uni tech"': 'مصروف الاهلاك "uni tech"',
    "warehouse rent": "ايجار مخزن",
    "maintenance and spare parts": "صيانة و قطع غيار بريموباك",
    "vehicle expenses": "م.سيارات",
    "forklift expenses": "م.كلاركات",
    "industrial security": "امن صناعي",
    "other expenses": "اخرى",  # bare "Other Expenses" -- appears in Production and Admin sections
    # Selling section
    "commissions and bank charges": "عمولات و مصروفات بنكية",
    "fees and licenses": "رسوم و تراخيص",  # selling section; admin section's own "رسوم و تراخيص" collides on name -- resolved by category-scoped lookup, see diff_workbook
    "exhibitions": "معارض",
    'other expenses "exports"': "اخرى",  # NOTE: Selling has two "اخرى" DB rows (Exports-context and General-context); both alias to the same Arabic name and are resolved positionally *within* that (category, name) group, see diff_workbook
    'other expenses "general"': "اخرى",
    "sales salaries": "رواتب بيع",
    # Admin section
    "administrative salaries": "رواتب ادارة",
    "administrative overtime": "اضافي",
    "travel and official assignments": "انتقالات وماموريات",  # NOTE: name collides with production's -- category-scoped
    "travel and official assignments": "انتقالات وماموريات",  # admin's phrasing of the same Arabic label production calls "Travel and Transportation"
    "professional fees and consultations": "اتعاب و استشارات",
    "senior management": "الادارة العليا",
    "utilities": "مرافق",
    "rent": "ايجار",
    "administrative depreciation": "مصروف الاهلاك الاداري",
    # Financial section
    "stamps and interest": "دمغات و فوائد",
    'stamps and interest "primo pack"': "فوائد و دمغات بريموباك",
}

# A handful of items share the exact same English label across two
# categories but were seeded under *different* Arabic names (only
# "Bonuses and Incentives" does this in practice -- production's is
# "مكافأت", admin's is the longer "مكافأت و حوافز"). Checked before the
# flat alias table above, keyed by (category, normalized English label).
CATEGORY_SCOPED_FIXED_COST_ALIASES = {
    ("admin", "bonuses and incentives"): "مكافأت و حوافز",
}
# Rows whose plain-English label collides with another section's (e.g. both
# Production and Admin have "Social Insurance" / "تأمينات اجتماعية", both
# Selling and Admin have a bare "Other Expenses" / "اخرى", and "مكافأت" vs
# "مكافأت و حوافز" differ only by category) are matched by (category, name)
# together in diff_workbook rather than by name alone, so the alias map
# above is intentionally silent on those -- the category-scoped exact-name
# match handles them directly without needing an alias.


def parse_fixed_cost_items(wb):
    """Walks the fixed-costs sheet top to bottom, tracking which category
    section we're in (production / selling / admin / financial, identified
    by section headers in either Arabic or English -- a later workbook may
    have the sheet tab and its section headers translated to English, e.g.
    'مصروفا صناعية' -> 'Manufacturing Expenses'), reading (name, value) rows
    under each, in sheet order (order is kept so diff_workbook can still
    fall back to position *within a same-named group*, e.g. the two
    "Other Expenses" rows in a section). Rows that are a section's own
    'Total ...' subtotal are skipped, not individual cost items."""
    ws = _sheet(wb, ["تكاليف ثابتة", "Fixed"])
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
        "manufacturing expenses": "production",
        "selling expenses": "selling",
        "general & administrative expenses": "admin",
        "general and administrative expenses": "admin",
        "finance costs": "financial",
        "financial expenses": "financial",
    }
    skip_labels = {"البيان", "الاجمالي", "description", "expense classification"}
    current = None
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        if not row or row[0] is None:
            continue
        first = row[0].strip() if isinstance(row[0], str) else None
        if first is None:
            continue
        if _norm(first) in section_headers:
            current = section_headers[_norm(first)]
            continue
        if _norm(first) in skip_labels or _norm(first).startswith("total") or current is None:
            continue
        value = row[2] if len(row) > 2 else None
        if isinstance(value, (int, float)):
            # Translate an English label back to the Arabic name the DB
            # was originally seeded with, so callers can match by name.
            # A (category, label) match wins over the flat table, for the
            # rare label that means a different DB row in a different
            # section (see CATEGORY_SCOPED_FIXED_COST_ALIASES).
            canonical = CATEGORY_SCOPED_FIXED_COST_ALIASES.get(
                (current, _norm(first)), FIXED_COST_LABEL_ALIASES.get(_norm(first), first)
            )
            out.append((current, canonical, float(value)))
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

    # Electricity: the variable tariff (EGP/kWh) and the per-micron/roll-type
    # kW/ton table both feed the Conversion Cost formula directly, so a
    # change here can move every product's price -- worth diffing even
    # though it isn't a simple flat-rate table like Material Rates.
    tariff, power = parse_electricity(wb)
    if tariff is not None:
        row = conn.execute("SELECT * FROM global_setting WHERE key='electricity_variable_tariff_egp_per_kwh'").fetchone()
        if row is not None:
            old_val = row["value"] or 0
            if abs(tariff - old_val) > 1e-9:
                changes.append(_change("Electricity", "Variable Tariff (EGP/kWh)", old_val, tariff,
                                        {"table": "global_setting", "id": "electricity_variable_tariff_egp_per_kwh", "value": tariff}))
            else:
                unchanged += 1
    for row in conn.execute("SELECT * FROM electricity_power").fetchall():
        key = (float(row["micron"]), row["roll_type"])
        new_val = power.get(key)
        if new_val is None:
            continue
        old_val = row["kw_per_ton"] or 0
        if abs(new_val - old_val) > 1e-6:
            changes.append(_change("Electricity", f"{row['roll_type']} {row['micron']}µ (kW/ton)", old_val, new_val,
                                    {"table": "electricity_power", "id": row["id"], "field": "kw_per_ton", "value": new_val}))
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

    # Fixed cost items -- matched by (category, name) using the Arabic
    # label the parser translates English rows back to (see
    # FIXED_COST_LABEL_ALIASES / parse_fixed_cost_items). Two different
    # category sections can legitimately share a name (e.g. both Production
    # and Admin have their own "تأمينات اجتماعية" / Social Insurance), which
    # (category, name) already disambiguates since each is matched only
    # within its own category. Within one category, the rare case of two
    # rows sharing the exact same name (Selling's two "اخرى" / "Other
    # Expenses" rows) falls back to matching in sheet order *within that
    # name's own group*, which is safe because same-name rows have no name
    # left to distinguish them by and the sheet's internal row order for a
    # repeated label is stable year to year.
    parsed_fixed = parse_fixed_cost_items(wb)
    parsed_by_cat_name = {}
    for cat, name, val in parsed_fixed:
        parsed_by_cat_name.setdefault((cat, _norm(name)), []).append(val)

    db_by_cat_name = {}
    for row in conn.execute("SELECT * FROM fixed_cost_item ORDER BY category, id").fetchall():
        db_by_cat_name.setdefault((row["category"], _norm(row["name"])), []).append(row)

    for key, db_rows in db_by_cat_name.items():
        cat, _ = key
        new_vals = parsed_by_cat_name.get(key)
        if not new_vals:
            continue  # no matching name found in the new workbook -- leave alone, don't guess
        for row, new_val in zip(db_rows, new_vals):
            old_val = row["value_egp"] or 0
            if abs(new_val - old_val) > 1e-6:
                changes.append(_change(
                    f"Fixed Costs ({cat})", row["name"], old_val, new_val,
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
