import io
import json
import os
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request, flash, jsonify,
    send_file, abort, session, g
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, init_db
from .pricing import compute_line, product_label, product_category
from . import cost_engine
from . import cost_upload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    with app.app_context():
        init_db()

    # ---------- request-scoped db + current user ----------
    @app.before_request
    def load_user():
        g.db = get_db()
        g.user = None
        user_id = session.get("user_id")
        if user_id:
            g.user = g.db.execute("SELECT * FROM user WHERE id=? AND active=1", (user_id,)).fetchone()

    @app.teardown_appcontext
    def close_db(exception=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def inject_globals():
        return {"current_user": g.get("user"), "now": datetime.now(timezone.utc)}

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if g.user["role"] != "admin":
                abort(403)
            return view(*args, **kwargs)
        return wrapped

    # ---------- Auth ----------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user is not None:
            return redirect(url_for("pricing_page"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            row = g.db.execute("SELECT * FROM user WHERE username=? AND active=1", (username,)).fetchone()
            if row and check_password_hash(row["password_hash"], password):
                session["user_id"] = row["id"]
                return redirect(url_for("pricing_page"))
            flash("Invalid username or password", "error")
        users = g.db.execute("SELECT * FROM user WHERE active=1 ORDER BY username").fetchall()
        return render_template("login.html", users=users)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------- Pricing / quote builder ----------
    @app.route("/pricing")
    @login_required
    def pricing_page():
        products_rows = g.db.execute(
            "SELECT * FROM product ORDER BY stretch_ability, CAST(micron AS REAL)"
        ).fetchall()
        products = [dict(p, label=product_label(p)) for p in products_rows]
        freight = g.db.execute("SELECT * FROM freight ORDER BY country").fetchall()
        pallet_types = ["Standard Pallet", "Euro Pallet"]
        packing_types = ["Automatic", "Manual(5kg)", "Manual(2.3~3.5kg)", "Manual(2.2kg)", "Manual(1.5kg)"]
        payment_terms = ["Cash (0 days)", "30 days", "60 days", "90 days"]
        pricing_bases = [("gross", "$/Roll (Gross)"), ("net", "$/Roll (Net)"), ("per_kg", "$/KG")]
        return render_template(
            "pricing.html",
            products=products,
            freight=freight,
            pallet_types=pallet_types,
            packing_types=packing_types,
            payment_terms=payment_terms,
            pricing_bases=pricing_bases,
        )

    @app.route("/api/calculate-line", methods=["POST"])
    @login_required
    def api_calculate_line():
        data = request.get_json(force=True)
        product = g.db.execute("SELECT * FROM product WHERE id=?", (data.get("product_id"),)).fetchone()
        if not product:
            return jsonify({"error": "Unknown product"}), 400
        country_class = data.get("country_class", "Moderate")
        customer_class = data.get("customer_class", "A")
        qty = float(data.get("quantity_pallets") or 0)
        pallet_type = data.get("pallet_type")
        pricing_basis = data.get("pricing_basis", "per_kg")
        adjustment = g.user["price_adjustment_usd_kg"] or 0
        unit_price, total_kg = compute_line(g.db, product, country_class, customer_class, qty,
                                             price_adjustment_usd_kg=adjustment, pallet_type=pallet_type,
                                             pricing_basis=pricing_basis)
        gross = round(unit_price * total_kg, 2)
        return jsonify({"unit_price_usd_kg": unit_price, "total_kg": total_kg, "line_gross": gross})

    @app.route("/api/save-quotation", methods=["POST"])
    @login_required
    def api_save_quotation():
        data = request.get_json(force=True)
        db = g.db
        q_id = data.get("id")

        if q_id:
            existing = db.execute("SELECT * FROM quotation WHERE id=?", (q_id,)).fetchone()
            if not existing or (g.user["role"] != "admin" and existing["created_by_id"] != g.user["id"]):
                abort(403)

        quotation_no = data.get("quotation_no") or None
        customer_name = data.get("customer_name")
        loading_port = data.get("loading_port")
        destination = data.get("destination")
        payment_term = data.get("payment_term")
        customer_class = data.get("customer_class", "A")
        country_class = data.get("country_class", "Moderate")
        seller_type = data.get("seller_type", "Foreign sellers")
        global_discount_pct = float(data.get("global_discount_pct") or 0)

        if q_id:
            db.execute(
                """UPDATE quotation SET quotation_no=?, customer_name=?, loading_port=?, destination=?,
                   payment_term=?, customer_class=?, country_class=?, seller_type=?, global_discount_pct=?,
                   status='saved' WHERE id=?""",
                (quotation_no, customer_name, loading_port, destination, payment_term,
                 customer_class, country_class, seller_type, global_discount_pct, q_id),
            )
            db.execute("DELETE FROM quotation_line WHERE quotation_id=?", (q_id,))
            quotation_id = q_id
        else:
            cur = db.execute(
                """INSERT INTO quotation
                   (quotation_no, customer_name, loading_port, destination, payment_term, customer_class,
                    country_class, seller_type, global_discount_pct, status, created_by_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'saved', ?, ?)""",
                (quotation_no, customer_name, loading_port, destination, payment_term, customer_class,
                 country_class, seller_type, global_discount_pct, g.user["id"],
                 datetime.now(timezone.utc).isoformat()),
            )
            quotation_id = cur.lastrowid

        creator_id = existing["created_by_id"] if q_id else g.user["id"]
        creator = db.execute("SELECT * FROM user WHERE id=?", (creator_id,)).fetchone()
        adjustment = (creator["price_adjustment_usd_kg"] if creator else 0) or 0

        for l in data.get("lines", []):
            product = db.execute("SELECT * FROM product WHERE id=?", (l.get("product_id"),)).fetchone()
            if not product:
                continue
            pallet_type = l.get("pallet_type", "Standard Pallet")
            pricing_basis = l.get("pricing_basis", "per_kg")
            unit_price, total_kg = compute_line(
                db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0),
                price_adjustment_usd_kg=adjustment, pallet_type=pallet_type, pricing_basis=pricing_basis,
            )
            db.execute(
                """INSERT INTO quotation_line
                   (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                    unit_price_usd_kg, total_kg, line_discount_pct, pricing_basis)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (quotation_id, product["id"], pallet_type,
                 l.get("packing_type", "Automatic"), float(l.get("quantity_pallets") or 0),
                 unit_price, total_kg, float(l.get("line_discount_pct") or 0), pricing_basis),
            )

        db.commit()
        q = db.execute("SELECT * FROM quotation WHERE id=?", (quotation_id,)).fetchone()
        total = compute_totals(db, q)["total"]
        return jsonify({"id": quotation_id, "quotation_no": q["quotation_no"], "total": total})

    # ---------- History ----------
    @app.route("/quotations")
    @login_required
    def history():
        db = g.db
        if g.user["role"] == "admin":
            rows = db.execute(
                """SELECT q.*, u.username as creator_username, u.full_name as creator_name
                   FROM quotation q LEFT JOIN user u ON u.id = q.created_by_id
                   ORDER BY q.created_at DESC"""
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT q.*, u.username as creator_username, u.full_name as creator_name
                   FROM quotation q LEFT JOIN user u ON u.id = q.created_by_id
                   WHERE q.created_by_id=? ORDER BY q.created_at DESC""",
                (g.user["id"],),
            ).fetchall()
        quotations = [dict(r, total=compute_totals(db, r)["total"]) for r in rows]
        return render_template("history.html", quotations=quotations)

    @app.route("/quotations/<int:qid>")
    @login_required
    def view_quotation(qid):
        q, lines, totals = load_quotation(g.db, qid)
        return render_template("view_quotation.html", q=q, lines=lines, totals=totals)

    @app.route("/quotations/<int:qid>/pdf")
    @login_required
    def quotation_pdf(qid):
        q, lines, totals = load_quotation(g.db, qid)
        buf = build_pdf(q, lines, totals)
        filename = f"Quotation_{q['quotation_no'] or q['id']}.pdf"
        return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/pdf")

    def load_quotation(db, qid):
        q = db.execute("SELECT * FROM quotation WHERE id=?", (qid,)).fetchone()
        if not q:
            abort(404)
        if g.user["role"] != "admin" and q["created_by_id"] != g.user["id"]:
            abort(403)
        line_rows = db.execute(
            """SELECT ql.*, p.stretch_ability, p.micron FROM quotation_line ql
               LEFT JOIN product p ON p.id = ql.product_id WHERE ql.quotation_id=?""",
            (qid,),
        ).fetchall()
        basis_labels = {"gross": "$/Roll (Gross)", "net": "$/Roll (Net)", "per_kg": "$/KG"}
        lines = []
        for l in line_rows:
            label = f"{l['micron']}μm – {l['stretch_ability']}" if l["stretch_ability"] else "-"
            gross = l["unit_price_usd_kg"] * l["total_kg"]
            line_total = round(gross * (1 - (l["line_discount_pct"] or 0) / 100), 2)
            basis = l["pricing_basis"] if "pricing_basis" in l.keys() and l["pricing_basis"] else "per_kg"
            lines.append(dict(l, label=label, line_total=line_total,
                               pricing_basis_label=basis_labels.get(basis, "$/KG")))
        totals = compute_totals(db, q, lines)
        return q, lines, totals

    def compute_totals(db, q, lines=None):
        if lines is None:
            line_rows = db.execute("SELECT * FROM quotation_line WHERE quotation_id=?", (q["id"],)).fetchall()
            lines = []
            for l in line_rows:
                gross = l["unit_price_usd_kg"] * l["total_kg"]
                lines.append({"line_total": round(gross * (1 - (l["line_discount_pct"] or 0) / 100), 2)})
        subtotal = round(sum(l["line_total"] for l in lines), 2)
        total = round(subtotal * (1 - (q["global_discount_pct"] or 0) / 100), 2)
        return {"subtotal": subtotal, "total": total}

    # ---------- Simple admin: users ----------
    @app.route("/admin/users", methods=["GET", "POST"])
    @admin_required
    def admin_users():
        db = g.db
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            full_name = request.form.get("full_name", "").strip()
            role = request.form.get("role", "sales_rep")
            region = request.form.get("region", "").strip()
            seller_type = request.form.get("seller_type", "local")
            password = request.form.get("password") or "ChangeMe123!"
            exists = db.execute("SELECT id FROM user WHERE username=?", (username,)).fetchone()
            if username and not exists:
                db.execute(
                    """INSERT INTO user (username, full_name, password_hash, role, region, seller_type)
                       VALUES (?,?,?,?,?,?)""",
                    (username, full_name, generate_password_hash(password), role, region, seller_type),
                )
                db.commit()
                flash(f"User {username} created.", "success")
            else:
                flash("Username missing or already exists.", "error")
            return redirect(url_for("admin_users"))
        users = db.execute("SELECT * FROM user ORDER BY username").fetchall()
        return render_template("admin_users.html", users=users)

    @app.route("/admin/users/<int:uid>/update", methods=["POST"])
    @admin_required
    def admin_user_update(uid):
        db = g.db
        db.execute(
            """UPDATE user SET full_name=?, role=?, region=?, active=?, price_adjustment_usd_kg=?,
               seller_type=? WHERE id=?""",
            (
                request.form.get("full_name", "").strip(),
                request.form.get("role", "sales_rep"),
                request.form.get("region", "").strip(),
                1 if request.form.get("active") == "on" else 0,
                float(request.form.get("price_adjustment_usd_kg") or 0),
                request.form.get("seller_type", "local"),
                uid,
            ),
        )
        db.commit()
        flash("User updated.", "success")
        return redirect(url_for("admin_users"))

    # ---------- Admin: rates (products / factors / freight) ----------
    @app.route("/admin/products", methods=["GET", "POST"])
    @admin_required
    def admin_products():
        db = g.db
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                db.execute(
                    """INSERT INTO product
                       (stretch_ability, micron, pallet_size, auto_manual, color, rolls_per_pallet,
                        roll_weight_kg, core_weight_kg, ex_work_usd_kg, fob_usd_kg, cfr_usd_kg)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        request.form.get("stretch_ability", "").strip(),
                        request.form.get("micron", "").strip(),
                        request.form.get("pallet_size", "Standard").strip(),
                        request.form.get("auto_manual", "Automatic").strip(),
                        request.form.get("color", "Transparent").strip(),
                        float(request.form.get("rolls_per_pallet") or 0),
                        float(request.form.get("roll_weight_kg") or 0),
                        float(request.form.get("core_weight_kg") or 0),
                        float(request.form.get("ex_work_usd_kg") or 0),
                        float(request.form.get("fob_usd_kg") or 0) or None,
                        float(request.form.get("cfr_usd_kg") or 0) or None,
                    ),
                )
                db.commit()
                flash("Product added.", "success")
            else:
                pid = request.form.get("product_id")
                db.execute(
                    """UPDATE product SET stretch_ability=?, micron=?, rolls_per_pallet=?, roll_weight_kg=?,
                       core_weight_kg=?, width_mm=?, ex_work_usd_kg=?, fob_usd_kg=?, cfr_usd_kg=? WHERE id=?""",
                    (
                        request.form.get("stretch_ability", "").strip(),
                        request.form.get("micron", "").strip(),
                        float(request.form.get("rolls_per_pallet") or 0),
                        float(request.form.get("roll_weight_kg") or 0),
                        float(request.form.get("core_weight_kg") or 0),
                        float(request.form.get("width_mm") or 0) or None,
                        float(request.form.get("ex_work_usd_kg") or 0),
                        float(request.form.get("fob_usd_kg") or 0) or None,
                        float(request.form.get("cfr_usd_kg") or 0) or None,
                        pid,
                    ),
                )
                db.commit()
                flash("Product updated.", "success")
            return redirect(url_for("admin_products"))
        products = db.execute(
            "SELECT * FROM product ORDER BY stretch_ability, CAST(micron AS REAL)"
        ).fetchall()
        return render_template("admin_products.html", products=products)

    @app.route("/admin/products/<int:pid>/delete", methods=["POST"])
    @admin_required
    def admin_product_delete(pid):
        g.db.execute("DELETE FROM product WHERE id=?", (pid,))
        g.db.commit()
        flash("Product deleted.", "success")
        return redirect(url_for("admin_products"))

    @app.route("/admin/factors", methods=["GET", "POST"])
    @admin_required
    def admin_factors():
        db = g.db
        if request.method == "POST":
            fid = request.form.get("factor_id")
            # The admin form takes/shows whole percentages (e.g. "15" = 15%);
            # the factor table itself still stores the decimal fraction
            # (0.15), exactly as the cost-engine math (unit_price_for)
            # expects -- this converts at the UI boundary only.
            def pct(name):
                return (float(request.form.get(name) or 0)) / 100.0
            db.execute(
                """UPDATE factor SET automatic_standard=?, automatic_power=?, automatic_power_plus=?,
                   uvi_standard=?, uvi_power=?, uvi_power_plus=?, regid=?, uv_regid=? WHERE id=?""",
                (
                    pct("automatic_standard"), pct("automatic_power"), pct("automatic_power_plus"),
                    pct("uvi_standard"), pct("uvi_power"), pct("uvi_power_plus"),
                    pct("regid"), pct("uv_regid"),
                    fid,
                ),
            )
            db.commit()
            flash("Factor updated.", "success")
            return redirect(url_for("admin_factors"))
        factors = db.execute(
            """SELECT * FROM factor ORDER BY
               CASE country_class WHEN 'High' THEN 1 WHEN 'Moderate' THEN 2 ELSE 3 END,
               customer_class,
               CASE roll_size WHEN 'standard' THEN 1 WHEN 'jumbo' THEN 2 ELSE 3 END"""
        ).fetchall()
        return render_template("admin_factors.html", factors=factors)

    @app.route("/admin/freight", methods=["GET", "POST"])
    @admin_required
    def admin_freight():
        db = g.db
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                country = request.form.get("country", "").strip()
                rate = request.form.get("shipping_rate_usd", "").strip()
                if country:
                    db.execute("INSERT INTO freight (country, shipping_rate_usd) VALUES (?, ?)", (country, rate))
                    db.commit()
                    flash("Freight route added.", "success")
            else:
                fid = request.form.get("freight_id")
                db.execute(
                    "UPDATE freight SET country=?, shipping_rate_usd=? WHERE id=?",
                    (request.form.get("country", "").strip(), request.form.get("shipping_rate_usd", "").strip(), fid),
                )
                db.commit()
                flash("Freight route updated.", "success")
            return redirect(url_for("admin_freight"))
        freight = db.execute("SELECT * FROM freight ORDER BY country").fetchall()
        return render_template("admin_freight.html", freight=freight)

    @app.route("/admin/freight/<int:fid>/delete", methods=["POST"])
    @admin_required
    def admin_freight_delete(fid):
        g.db.execute("DELETE FROM freight WHERE id=?", (fid,))
        g.db.commit()
        flash("Freight route deleted.", "success")
        return redirect(url_for("admin_freight"))

    @app.route("/admin/products/recalculate", methods=["POST"])
    @admin_required
    def admin_products_recalculate():
        cost_engine.recalculate_all_products(g.db)
        flash("EX-Work cost recalculated for all products from current cost-engine inputs.", "success")
        return redirect(url_for("admin_products"))

    # ---------- Admin: cost engine (raw costs behind EX-Work) ----------
    @app.route("/admin/cost/global-settings", methods=["GET", "POST"])
    @admin_required
    def admin_global_settings():
        db = g.db
        if request.method == "POST":
            for row in db.execute("SELECT key FROM global_setting").fetchall():
                key = row["key"]
                val = request.form.get(f"value_{key}")
                if val is not None and val != "":
                    db.execute("UPDATE global_setting SET value=? WHERE key=?", (float(val), key))
            db.commit()
            flash("Global cost settings updated.", "success")
            return redirect(url_for("admin_global_settings"))
        settings = db.execute("SELECT * FROM global_setting ORDER BY label").fetchall()
        return render_template("admin_global_settings.html", settings=settings)

    @app.route("/admin/cost/materials", methods=["GET", "POST"])
    @admin_required
    def admin_material_rates():
        db = g.db
        if request.method == "POST":
            for row in db.execute("SELECT id FROM material_rate").fetchall():
                val = request.form.get(f"value_{row['id']}")
                if val is not None and val != "":
                    db.execute("UPDATE material_rate SET value=? WHERE id=?", (float(val), row["id"]))
            db.commit()
            flash("Material rates updated.", "success")
            return redirect(url_for("admin_material_rates"))
        resin = db.execute("SELECT * FROM material_rate WHERE category='resin' ORDER BY label").fetchall()
        packaging = db.execute("SELECT * FROM material_rate WHERE category='packaging' ORDER BY label").fetchall()
        return render_template("admin_material_rates.html", resin=resin, packaging=packaging)

    @app.route("/admin/cost/labor", methods=["GET", "POST"])
    @admin_required
    def admin_labor():
        db = g.db
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                db.execute(
                    "INSERT INTO labor_employee (name, role, base_2023_egp, increase_rate) VALUES (?,?,?,?)",
                    (request.form.get("name", "").strip(), request.form.get("role", "").strip(),
                     float(request.form.get("base_2023_egp") or 0), float(request.form.get("increase_rate") or 0)),
                )
                db.commit()
                flash("Employee added.", "success")
            else:
                eid = request.form.get("employee_id")
                db.execute(
                    """UPDATE labor_employee SET name=?, role=?, base_2023_egp=?, increase_rate=?,
                       active=? WHERE id=?""",
                    (request.form.get("name", "").strip(), request.form.get("role", "").strip(),
                     float(request.form.get("base_2023_egp") or 0), float(request.form.get("increase_rate") or 0),
                     1 if request.form.get("active") == "on" else 0, eid),
                )
                db.commit()
                flash("Employee updated.", "success")
            cost_engine.sync_labor_to_fixed_costs(db)
            return redirect(url_for("admin_labor"))
        employees = db.execute("SELECT * FROM labor_employee ORDER BY active DESC, name").fetchall()
        fixed_total = sum((e["base_2023_egp"] or 0) * (1 + (e["increase_rate"] or 0)) for e in employees if e["active"])
        variable_total = sum(((e["base_2023_egp"] or 0) * (1 + (e["increase_rate"] or 0)) / 8) * 4
                              for e in employees if e["active"])
        return render_template("admin_labor.html", employees=employees,
                                fixed_total=round(fixed_total, 2), variable_total=round(variable_total, 2))

    @app.route("/admin/cost/labor/<int:eid>/delete", methods=["POST"])
    @admin_required
    def admin_labor_delete(eid):
        g.db.execute("DELETE FROM labor_employee WHERE id=?", (eid,))
        g.db.commit()
        cost_engine.sync_labor_to_fixed_costs(g.db)
        flash("Employee removed.", "success")
        return redirect(url_for("admin_labor"))

    @app.route("/admin/cost/electricity", methods=["GET", "POST"])
    @admin_required
    def admin_electricity():
        db = g.db
        if request.method == "POST":
            table = request.form.get("table")
            rid = request.form.get("row_id")
            if table == "power":
                val = request.form.get("kw_per_ton")
                db.execute("UPDATE electricity_power SET kw_per_ton=? WHERE id=?", (float(val or 0), rid))
            elif table == "capacity":
                val = request.form.get("tons_per_day")
                db.execute("UPDATE production_capacity SET tons_per_day=? WHERE id=?", (float(val or 0), rid))
            db.commit()
            flash("Electricity data updated.", "success")
            return redirect(url_for("admin_electricity"))
        power = db.execute("SELECT * FROM electricity_power ORDER BY roll_type, micron").fetchall()
        capacity = db.execute("SELECT * FROM production_capacity ORDER BY roll_type, micron").fetchall()
        return render_template("admin_electricity.html", power=power, capacity=capacity)

    @app.route("/admin/cost/variable-costs", methods=["GET", "POST"])
    @admin_required
    def admin_variable_costs():
        db = g.db
        if request.method == "POST":
            for row in db.execute("SELECT id FROM variable_cost_item").fetchall():
                val = request.form.get(f"value_{row['id']}")
                if val is not None and val != "":
                    db.execute("UPDATE variable_cost_item SET value_egp_per_ton=? WHERE id=?",
                               (float(val), row["id"]))
            db.commit()
            flash("Variable cost items updated.", "success")
            return redirect(url_for("admin_variable_costs"))
        items = db.execute("SELECT * FROM variable_cost_item ORDER BY id").fetchall()
        return render_template("admin_variable_costs.html", items=items)

    @app.route("/admin/cost/fixed-costs", methods=["GET", "POST"])
    @admin_required
    def admin_fixed_costs():
        db = g.db
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                db.execute(
                    "INSERT INTO fixed_cost_item (category, name, value_egp) VALUES (?,?,?)",
                    (request.form.get("category", "production"), request.form.get("name", "").strip(),
                     float(request.form.get("value_egp") or 0)),
                )
                db.commit()
                flash("Fixed cost item added.", "success")
            else:
                for row in db.execute("SELECT id FROM fixed_cost_item").fetchall():
                    val = request.form.get(f"value_{row['id']}")
                    if val is not None and val != "":
                        db.execute("UPDATE fixed_cost_item SET value_egp=? WHERE id=?", (float(val), row["id"]))
                db.commit()
                flash("Fixed cost items updated.", "success")
            return redirect(url_for("admin_fixed_costs"))
        items = db.execute("SELECT * FROM fixed_cost_item ORDER BY category, id").fetchall()
        by_category = {}
        for it in items:
            by_category.setdefault(it["category"], []).append(it)
        total = sum(it["value_egp"] or 0 for it in items)
        return render_template("admin_fixed_costs.html", by_category=by_category, total=round(total, 2))

    @app.route("/admin/cost/fixed-costs/<int:fid>/delete", methods=["POST"])
    @admin_required
    def admin_fixed_cost_delete(fid):
        g.db.execute("DELETE FROM fixed_cost_item WHERE id=?", (fid,))
        g.db.commit()
        flash("Fixed cost item removed.", "success")
        return redirect(url_for("admin_fixed_costs"))

    @app.route("/admin/cost/pallet", methods=["GET", "POST"])
    @admin_required
    def admin_pallet():
        db = g.db
        if request.method == "POST":
            pid = request.form.get("pallet_id")
            db.execute(
                """UPDATE pallet_component SET pallet_qty=?, cardboard_qty=?, cap_qty=?, corrugated_kg=?,
                   stretch_kg=?, box_qty=?, rolls_per_box=?, cartoon_angle_qty=?, scotch_tape_qty=? WHERE id=?""",
                (
                    float(request.form.get("pallet_qty") or 0), float(request.form.get("cardboard_qty") or 0),
                    float(request.form.get("cap_qty") or 0), float(request.form.get("corrugated_kg") or 0),
                    float(request.form.get("stretch_kg") or 0), float(request.form.get("box_qty") or 0),
                    float(request.form.get("rolls_per_box") or 0), float(request.form.get("cartoon_angle_qty") or 0),
                    float(request.form.get("scotch_tape_qty") or 0), pid,
                ),
            )
            db.commit()
            flash("Pallet / packaging component updated.", "success")
            return redirect(url_for("admin_pallet"))
        rows = db.execute("SELECT * FROM pallet_component ORDER BY label").fetchall()
        totals = {r["packing_key"]: round(cost_engine.pallet_component_total_usd(db, r["packing_key"]), 4)
                   for r in rows}
        return render_template("admin_pallet.html", rows=rows, totals=totals)

    @app.route("/admin/cost/bom", methods=["GET", "POST"])
    @admin_required
    def admin_bom():
        db = g.db
        if request.method == "POST":
            bid = request.form.get("bom_id")
            db.execute(
                """UPDATE bom_row SET exceed3518=?, exceed3812=?, exceedxp=?, vista6000=?, enable=?,
                   ld258=?, vista6202=? WHERE id=?""",
                (
                    float(request.form.get("exceed3518") or 0), float(request.form.get("exceed3812") or 0),
                    float(request.form.get("exceedxp") or 0), float(request.form.get("vista6000") or 0),
                    float(request.form.get("enable") or 0), float(request.form.get("ld258") or 0),
                    float(request.form.get("vista6202") or 0), bid,
                ),
            )
            db.commit()
            flash("BOM row updated.", "success")
            return redirect(url_for("admin_bom"))
        rows = db.execute(
            "SELECT * FROM bom_row ORDER BY stretch_multiplier, roll_tier, micron"
        ).fetchall()
        return render_template("admin_bom.html", rows=rows)

    @app.route("/admin/cost/packing-tiers", methods=["GET", "POST"])
    @admin_required
    def admin_packing_tiers():
        db = g.db
        if request.method == "POST":
            tid = request.form.get("tier_id")
            def num(name):
                v = request.form.get(name)
                return float(v) if v not in (None, "") else None
            db.execute(
                """UPDATE packing_tier SET match_weight_kg=?, rolls_per_box=?, box_per_pallet=?,
                   rolls_per_pallet=?, pallets_per_container40=?, pallets_per_container20=? WHERE id=?""",
                (
                    num("match_weight_kg") or 0, num("rolls_per_box"), num("box_per_pallet"),
                    num("rolls_per_pallet") or 0, num("pallets_per_container40"), num("pallets_per_container20"),
                    tid,
                ),
            )
            db.commit()
            flash("Packing tier updated.", "success")
            return redirect(url_for("admin_packing_tiers"))
        rows = db.execute(
            "SELECT * FROM packing_tier ORDER BY category, pallet_type, match_weight_kg"
        ).fetchall()
        return render_template("admin_packing_tiers.html", rows=rows)

    # ---------- Admin: annual cost upload (diff-review-confirm) ----------
    @app.route("/admin/cost-upload", methods=["GET", "POST"])
    @admin_required
    def admin_cost_upload():
        db = g.db
        if request.method == "POST" and "workbook" in request.files:
            f = request.files["workbook"]
            if not f or not f.filename:
                flash("Choose an .xlsx file first.", "error")
                return redirect(url_for("admin_cost_upload"))
            import tempfile
            tmp_path = os.path.join(tempfile.gettempdir(), f"cost_upload_{session.get('user_id')}.xlsx")
            f.save(tmp_path)
            try:
                changes, unchanged_count = cost_upload.diff_workbook(db, tmp_path)
            except Exception as e:
                flash(f"Could not read that workbook: {e}", "error")
                return redirect(url_for("admin_cost_upload"))
            session["cost_upload_path"] = tmp_path
            session["cost_upload_filename"] = f.filename
            return render_template("admin_cost_upload.html", stage="review", changes=changes,
                                    unchanged_count=unchanged_count, filename=f.filename)

        if request.method == "POST" and request.form.get("action") == "apply":
            tmp_path = session.get("cost_upload_path")
            filename = session.get("cost_upload_filename", "upload.xlsx")
            if not tmp_path or not os.path.exists(tmp_path):
                flash("Upload session expired -- please upload the file again.", "error")
                return redirect(url_for("admin_cost_upload"))
            changes, unchanged_count = cost_upload.diff_workbook(db, tmp_path)
            cost_upload.apply_changes(db, changes)
            db.execute(
                "INSERT INTO cost_upload_log (created_at, created_by, filename, summary_json) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), g.user["username"], filename,
                 cost_upload.summarize_for_log(changes)),
            )
            db.commit()
            cost_engine.recalculate_all_products(db)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            session.pop("cost_upload_path", None)
            session.pop("cost_upload_filename", None)
            flash(f"Applied {len(changes)} change(s) from {filename}. Product EX-Work costs recalculated.",
                  "success")
            return redirect(url_for("admin_cost_upload"))

        recent_rows = db.execute(
            "SELECT * FROM cost_upload_log ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        recent = []
        for r in recent_rows:
            try:
                n = len(json.loads(r["summary_json"] or "[]"))
            except Exception:
                n = 0
            recent.append(dict(r, change_count=n))
        return render_template("admin_cost_upload.html", stage="upload", recent=recent)

    @app.route("/admin/cost/preview")
    @admin_required
    def admin_cost_preview():
        db = g.db
        products = db.execute(
            "SELECT * FROM product ORDER BY stretch_ability, CAST(micron AS REAL)"
        ).fetchall()
        rows = []
        for p in products:
            bd = cost_engine.breakdown(db, p)
            rows.append({"product": p, "label": product_label(p), "breakdown": bd})
        return render_template("admin_cost_preview.html", rows=rows)

    return app


COMPANY_NAME = "ALEX INTERNATIONAL FOR PLASTIC INDUSTRY"
COMPANY_ADDRESS = "Plot (37), Block(B), New Investors Area, Petrochemicals route, Merghem Quebly, Alexandria, Egypt"
COMPANY_TEL = "Head office: (+203) 4241482    Fax: (+203) 4241482"
COMPANY_TEL2 = "Factory: (+203) 9680813"
COMPANY_TEL3 = "Main Stores: (+203) 3600803"
COMPANY_FAX = "(+203) 3601229"
COMPANY_EMAIL = "info@clipsicopack.com"


def build_pdf(q, lines, totals):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=14 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#1a1a1a"))
    company_name_style = ParagraphStyle("CoName", parent=styles["Normal"], fontSize=15, fontName="Helvetica-Bold",
                                         textColor=colors.HexColor("#1a1a1a"))
    company_detail_style = ParagraphStyle("CoDetail", parent=styles["Normal"], fontSize=8,
                                           textColor=colors.HexColor("#444444"), leading=11)

    logo_path = os.path.join(BASE_DIR, "static", "logo.png")
    company_lines = [
        Paragraph(COMPANY_NAME, company_name_style),
        Paragraph(f"Address&nbsp;&nbsp;: {COMPANY_ADDRESS}", company_detail_style),
        Paragraph(f"Tel&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: {COMPANY_TEL}", company_detail_style),
        Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{COMPANY_TEL2}", company_detail_style),
        Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{COMPANY_TEL3}", company_detail_style),
        Paragraph(f"Fax&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: {COMPANY_FAX}", company_detail_style),
        Paragraph(f"Email&nbsp;&nbsp;&nbsp;: {COMPANY_EMAIL}", company_detail_style),
    ]
    company_table = Table([[p] for p in company_lines], colWidths=[148 * mm])
    company_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))

    if os.path.exists(logo_path):
        logo = RLImage(logo_path, width=26 * mm, height=26 * mm * (246 / 209))
        letterhead = Table([[logo, company_table]], colWidths=[32 * mm, 148 * mm])
        letterhead.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("LEFTPADDING", (1, 0), (1, 0), 6),
        ]))
    else:
        letterhead = company_table

    elements = [letterhead, Spacer(1, 10)]
    elements.append(Table([[""]], colWidths=[180 * mm], rowHeights=[0.75],
                           style=TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1, colors.HexColor("#cccccc"))])))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph("Quotation", title_style))
    elements.append(Spacer(1, 6))

    created_at = q["created_at"] or ""
    date_str = created_at[:10] if created_at else "-"
    meta = [
        ["Quotation No.", q["quotation_no"] or f"#{q['id']}", "Date", date_str],
        ["Customer", q["customer_name"] or "-", "Payment Term", q["payment_term"] or "-"],
        ["Loading Port", q["loading_port"] or "-", "Destination", q["destination"] or "-"],
        ["Customer Class.", q["customer_class"] or "-", "Discount", f"{q['global_discount_pct'] or 0}%"],
    ]
    meta_table = Table(meta, colWidths=[90, 150, 90, 150])
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(meta_table)
    elements.append(Spacer(1, 14))

    header = ["#", "Product", "Pallet", "Packing", "Basis", "Qty (pallets)", "Total KG", "Unit $/KG", "Line Total $"]
    rows = [header]
    for i, line in enumerate(lines, start=1):
        rows.append([
            str(i), line["label"], line["pallet_type"], line["packing_type"],
            line.get("pricing_basis_label", "$/KG"),
            f"{line['quantity_pallets']:g}", f"{line['total_kg']:,.1f}",
            f"{line['unit_price_usd_kg']:.3f}", f"{line['line_total']:,.2f}",
        ])
    table = Table(rows, colWidths=[16, 108, 58, 58, 62, 48, 48, 48, 60])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))

    totals_rows = [
        ["Subtotal", f"${totals['subtotal']:,.2f}"],
        [f"Global Discount ({q['global_discount_pct'] or 0}%)",
         f"-${round(totals['subtotal'] - totals['total'], 2):,.2f}"],
        ["Total", f"${totals['total']:,.2f}"],
    ]
    totals_table = Table(totals_rows, colWidths=[400, 90])
    totals_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
    ]))
    elements.append(totals_table)

    doc.build(elements)
    buf.seek(0)
    return buf
