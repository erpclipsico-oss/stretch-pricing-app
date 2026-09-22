import io
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
        return render_template(
            "pricing.html",
            products=products,
            freight=freight,
            pallet_types=pallet_types,
            packing_types=packing_types,
            payment_terms=payment_terms,
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
        unit_price, total_kg = compute_line(g.db, product, country_class, customer_class, qty)
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

        for l in data.get("lines", []):
            product = db.execute("SELECT * FROM product WHERE id=?", (l.get("product_id"),)).fetchone()
            if not product:
                continue
            unit_price, total_kg = compute_line(
                db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0)
            )
            db.execute(
                """INSERT INTO quotation_line
                   (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                    unit_price_usd_kg, total_kg, line_discount_pct)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (quotation_id, product["id"], l.get("pallet_type", "Standard Pallet"),
                 l.get("packing_type", "Automatic"), float(l.get("quantity_pallets") or 0),
                 unit_price, total_kg, float(l.get("line_discount_pct") or 0)),
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
        lines = []
        for l in line_rows:
            label = f"{l['micron']}μm – {l['stretch_ability']}" if l["stretch_ability"] else "-"
            gross = l["unit_price_usd_kg"] * l["total_kg"]
            line_total = round(gross * (1 - (l["line_discount_pct"] or 0) / 100), 2)
            lines.append(dict(l, label=label, line_total=line_total))
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
            password = request.form.get("password") or "ChangeMe123!"
            exists = db.execute("SELECT id FROM user WHERE username=?", (username,)).fetchone()
            if username and not exists:
                db.execute(
                    "INSERT INTO user (username, full_name, password_hash, role, region) VALUES (?,?,?,?,?)",
                    (username, full_name, generate_password_hash(password), role, region),
                )
                db.commit()
                flash(f"User {username} created.", "success")
            else:
                flash("Username missing or already exists.", "error")
            return redirect(url_for("admin_users"))
        users = db.execute("SELECT * FROM user ORDER BY username").fetchall()
        return render_template("admin_users.html", users=users)

    return app


def build_pdf(q, lines, totals):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#1a1a1a"))
    elements = [Paragraph("Quotation", title_style), Spacer(1, 6)]

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

    header = ["#", "Product", "Pallet", "Packing", "Qty (pallets)", "Total KG", "Unit $/KG", "Line Total $"]
    rows = [header]
    for i, line in enumerate(lines, start=1):
        rows.append([
            str(i), line["label"], line["pallet_type"], line["packing_type"],
            f"{line['quantity_pallets']:g}", f"{line['total_kg']:,.1f}",
            f"{line['unit_price_usd_kg']:.3f}", f"{line['line_total']:,.2f}",
        ])
    table = Table(rows, colWidths=[20, 130, 70, 70, 60, 55, 55, 65])
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
