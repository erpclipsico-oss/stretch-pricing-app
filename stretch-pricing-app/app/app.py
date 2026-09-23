import io
import json
import os
import re
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request, flash, jsonify,
    send_file, abort, session, g
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, init_db
from .pricing import (
    compute_line, product_label, disambiguate_labels, product_category,
    is_prestretch, compute_prestretch_line,
)
from . import cost_engine
from . import cost_upload
from . import table_sync
from . import strap_pricing

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
        # v39 -- no more "(16kg roll)"/"(50kg roll)" suffix on the product
        # dropdown, per the owner: the Roll KG field next to it is freely
        # editable for every line anyway, so the catalog's own default
        # weight isn't a meaningful distinguishing label to her. Two
        # catalog rows with the same micron+Stretch Ability now show the
        # exact same text in the dropdown; disambiguate_labels() is still
        # used elsewhere (e.g. admin_cost_preview) where that matters.
        products = [
            dict(p, label=product_label(p), is_prestretch=is_prestretch(p))
            for p in products_rows
        ]
        strap_products_rows = g.db.execute(
            "SELECT * FROM strap_product ORDER BY line_key, code"
        ).fetchall()
        strap_products = [
            dict(p, label=f"{strap_pricing.LINE_CONFIG[p['line_key']]['label']} – {p['code']}")
            for p in strap_products_rows
        ]
        freight = g.db.execute("SELECT * FROM freight ORDER BY country").fetchall()
        loading_ports = g.db.execute("SELECT * FROM loading_port ORDER BY port").fetchall()
        pallet_types = ["Standard Pallet", "Euro Pallet"]
        packing_types = ["Automatic", "Manual(5kg)", "Manual(2.3~3.5kg)", "Manual(2.2kg)", "Manual(1.5kg)"]
        payment_terms = ["Cash (0 days)", "30 days", "60 days", "90 days"]
        pricing_bases = [("gross", "$/Roll (Gross)"), ("net", "$/Roll (Net)"), ("per_kg", "$/KG")]
        prestretch_packaging_types = [("no_boxes", "No Boxes"), ("boxes", "With Boxes")]
        # v37 -- live client-side g/m preview (width x thickness -> g/m,
        # instantly, no round-trip) needs each BOM recipe's own component
        # fractions (PP's density formula depends on them; PET's doesn't but
        # gets the same treatment for consistency). Read live from the DB so
        # it always reflects whatever's currently on the Strap Costing admin
        # page, not a frozen snapshot.
        strap_bom_components = {}
        for line_key in ("pet", "pp"):
            strap_bom_components[line_key] = {
                row["bom_key"]: json.loads(row["components_json"])
                for row in g.db.execute("SELECT bom_key, components_json FROM strap_bom WHERE line_key=?",
                                         (line_key,)).fetchall()
            }
        return render_template(
            "pricing.html",
            products=products,
            strap_products=strap_products,
            strap_bom_labels=strap_pricing.BOM_LABELS,
            strap_bom_components=strap_bom_components,
            strap_core_sizes=list(strap_pricing.CORE_SIZES_MM.items()),
            freight=freight,
            loading_ports=loading_ports,
            pallet_types=pallet_types,
            packing_types=packing_types,
            payment_terms=payment_terms,
            pricing_bases=pricing_bases,
            prestretch_packaging_types=prestretch_packaging_types,
            uv_types=cost_engine.UV_TYPES,
        )

    @app.route("/api/calculate-line", methods=["POST"])
    @login_required
    def api_calculate_line():
        data = request.get_json(force=True)
        product_line = data.get("product_line") or "stretch_film"

        if product_line in ("pet", "pp"):
            return _calculate_strap_line(data, product_line)

        product = g.db.execute("SELECT * FROM product WHERE id=?", (data.get("product_id"),)).fetchone()
        if not product:
            return jsonify({"error": "Unknown product"}), 400
        country_class = data.get("country_class", "Moderate")
        customer_class = data.get("customer_class", "A")
        qty = float(data.get("quantity_pallets") or 0)
        pallet_type = data.get("pallet_type")
        pricing_basis = data.get("pricing_basis", "per_kg")
        adjustment = g.user["price_adjustment_usd_kg"] or 0
        seller_type = g.user["seller_type"] if "seller_type" in g.user.keys() else None
        colored = bool(data.get("colored"))
        # v39 -- UV is now a plain checkbox ("uv": true/false); which of the
        # 7 UVI_TYPES variants applies is derived from this same product's
        # own Stretch Ability (see cost_engine.uv_type_for_product()), not
        # picked separately.
        uv_type = cost_engine.uv_type_for_product(product["stretch_ability"]) if data.get("uv") else None
        # v27: Discount % now comes off the margin factor (see pricing.py's
        # _discounted_factor()), not off the finished price -- so the
        # line's own Discount % and the quotation's Global Discount % are
        # combined here (added, in percentage points) and threaded straight
        # into the price computation, rather than applied afterwards.
        line_discount_pct = float(data.get("line_discount_pct") or 0)
        global_discount_pct = float(data.get("global_discount_pct") or 0)
        discount_pct = line_discount_pct + global_discount_pct

        if is_prestretch(product):
            roll_weight_kg = float(data.get("prestretch_roll_weight_kg") or 0)
            core_weight_kg = float(data.get("prestretch_core_weight_kg") or 0)
            rolls_per_pallet = float(data.get("prestretch_rolls_per_pallet") or 0)
            packaging_type = data.get("prestretch_packaging_type", "no_boxes")
            unit_price, total_kg = compute_prestretch_line(
                g.db, product, country_class, customer_class, qty, roll_weight_kg, core_weight_kg,
                rolls_per_pallet, packaging_type, price_adjustment_usd_kg=adjustment, pricing_basis=pricing_basis,
                seller_type=seller_type, colored=colored, discount_pct=discount_pct,
            )
            unit_price_full, _ = compute_prestretch_line(
                g.db, product, country_class, customer_class, qty, roll_weight_kg, core_weight_kg,
                rolls_per_pallet, packaging_type, price_adjustment_usd_kg=adjustment, pricing_basis=pricing_basis,
                seller_type=seller_type, colored=colored, discount_pct=0,
            )
            gross = cost_engine.round_half_up(unit_price * total_kg, 2)
            gross_full = cost_engine.round_half_up(unit_price_full * total_kg, 2)
            return jsonify({
                "unit_price_usd_kg": unit_price,
                "unit_price_full_usd_kg": unit_price_full,
                "total_kg": total_kg,
                "line_gross": gross,
                "line_gross_full": gross_full,
                "roll_weight_kg": roll_weight_kg,
                "core_weight_kg": core_weight_kg,
                "rolls_per_pallet": rolls_per_pallet,
                "pallets_per_container40": None,
                "pallets_per_container20": None,
            })

        custom_roll_weight_kg = data.get("custom_roll_weight_kg")
        custom_core_weight_kg = data.get("custom_core_weight_kg")
        custom_width_mm = data.get("custom_width_mm")
        custom_rolls_per_pallet = data.get("custom_rolls_per_pallet")
        # v21.1: the line's own Packing type selection now drives the price
        # (see cost_engine.with_overrides()'s auto_manual param) instead of
        # always pricing off the selected product's own catalog Auto/Manual.
        auto_manual_override = data.get("packing_type") or None
        unit_price, total_kg = compute_line(g.db, product, country_class, customer_class, qty,
                                             price_adjustment_usd_kg=adjustment, pallet_type=pallet_type,
                                             pricing_basis=pricing_basis,
                                             roll_weight_kg=custom_roll_weight_kg,
                                             core_weight_kg=custom_core_weight_kg,
                                             width_mm=custom_width_mm,
                                             rolls_per_pallet_override=custom_rolls_per_pallet,
                                             seller_type=seller_type,
                                             auto_manual_override=auto_manual_override, colored=colored,
                                             discount_pct=discount_pct, uv_type=uv_type)
        unit_price_full, _ = compute_line(g.db, product, country_class, customer_class, qty,
                                           price_adjustment_usd_kg=adjustment, pallet_type=pallet_type,
                                           pricing_basis=pricing_basis,
                                           roll_weight_kg=custom_roll_weight_kg,
                                           core_weight_kg=custom_core_weight_kg,
                                           width_mm=custom_width_mm,
                                           rolls_per_pallet_override=custom_rolls_per_pallet,
                                           seller_type=seller_type,
                                           auto_manual_override=auto_manual_override, colored=colored,
                                           discount_pct=0, uv_type=uv_type)
        gross = cost_engine.round_half_up(unit_price * total_kg, 2)
        gross_full = cost_engine.round_half_up(unit_price_full * total_kg, 2)
        effective_product = cost_engine.with_overrides(product, custom_roll_weight_kg, custom_core_weight_kg,
                                                         custom_width_mm, auto_manual=auto_manual_override)
        rolls_per_pallet = cost_engine.effective_rolls_per_pallet(g.db, effective_product, pallet_type,
                                                                    custom_rolls_per_pallet)
        tier = cost_engine.lookup_packing_tier(g.db, effective_product["auto_manual"],
                                                effective_product["roll_weight_kg"], pallet_type)
        return jsonify({
            "unit_price_usd_kg": unit_price,
            "unit_price_full_usd_kg": unit_price_full,
            "total_kg": total_kg,
            "line_gross": gross,
            "line_gross_full": gross_full,
            "roll_weight_kg": effective_product["roll_weight_kg"] or 0,
            "core_weight_kg": effective_product["core_weight_kg"] or 0,
            "width_mm": effective_product["width_mm"] if "width_mm" in effective_product else 0,
            "rolls_per_pallet": rolls_per_pallet,
            "pallets_per_container40": (tier["pallets_per_container40"] if tier else None),
            "pallets_per_container20": (tier["pallets_per_container20"] if tier else None),
        })

    def _strap_credit_term(data):
        """Reuses the quotation's own Payment Term selector: anything other
        than 'Cash (...)' triggers the strap sheets' flat $/kg credit-term
        surcharge, exactly like the payment_term field already does for the
        rest of the quotation."""
        payment_term = (data.get("payment_term") or "").strip().lower()
        return bool(payment_term) and not payment_term.startswith("cash")

    def _build_custom_strap_product(data, product_line):
        """v32 -- "Custom (width x thickness)" strap line: the rep picks a
        material class (pure/recycled x colored/not) and types width +
        thickness instead of a catalog code; core weight comes from the
        core-diameter lookup (owner-confirmed: 150mm=0.25kg, 200mm=0.5kg,
        400-405mm=1kg -- see strap_pricing.CORE_SIZES_MM). If meters/coil
        isn't given (or is 0), it's auto-suggested to land the gross roll
        weight at the top of the line's target window without exceeding
        it (strap_pricing.suggest_meters_per_coil)."""
        bom_key = data.get("strap_bom_key")
        if bom_key not in strap_pricing.BOM_KEYS.get(product_line, []):
            return None, jsonify({"error": "Unknown material class"}), 400
        width_mm = float(data.get("strap_width_mm") or 0)
        thickness_mm = float(data.get("strap_thickness_mm") or 0)
        core_size = str(data.get("strap_core_size") or "")
        core_weight_kg = strap_pricing.CORE_SIZES_MM.get(core_size)
        if core_weight_kg is None:
            return None, jsonify({"error": "Unknown core size"}), 400
        has_box = bool(data.get("strap_has_box"))
        has_pallet = bool(data.get("strap_has_pallet", True))
        ctr20 = bool(data.get("strap_ctr20"))
        ctr40 = bool(data.get("strap_ctr40"))
        if not (ctr20 or ctr40):
            ctr40 = True  # a container type is required for FOB/CFR; default 40ft

        gm_per_m = strap_pricing.meter_weight_g_per_m(
            product_line, {"width_mm": width_mm, "thickness_mm": thickness_mm},
            strap_pricing._get_bom(g.db, product_line, bom_key)["components"],
        )
        meters_per_coil = float(data.get("strap_meters_per_coil") or 0)
        if meters_per_coil <= 0:
            meters_per_coil = strap_pricing.suggest_meters_per_coil(product_line, gm_per_m, core_weight_kg)

        product = {
            "bom_key": bom_key, "width_mm": width_mm, "thickness_mm": thickness_mm,
            "meters_per_coil": meters_per_coil, "core_weight_kg": core_weight_kg,
            "has_box": has_box, "has_pallet": has_pallet, "ctr20": ctr20, "ctr40": ctr40,
        }
        return product, None, None

    def _calculate_strap_line(data, product_line):
        if data.get("strap_custom"):
            product, err_resp, err_code = _build_custom_strap_product(data, product_line)
            if product is None:
                return err_resp, err_code
        else:
            product = g.db.execute(
                "SELECT * FROM strap_product WHERE id=? AND line_key=?",
                (data.get("strap_product_id"), product_line),
            ).fetchone()
            if not product:
                return jsonify({"error": "Unknown strap product"}), 400
        qty_coils = float(data.get("quantity_coils") or 0)
        line_discount_pct = float(data.get("line_discount_pct") or 0)
        global_discount_pct = float(data.get("global_discount_pct") or 0)
        discount_pct = line_discount_pct + global_discount_pct
        credit_term = _strap_credit_term(data)
        # Hidden per-user strap markup (e.g. Manuel/Pasquale) -- see
        # user.strap_markup_pct and strap_pricing.compute_strap_line().
        hidden_markup_pct = (g.user["strap_markup_pct"] if "strap_markup_pct" in g.user.keys() else 0) or 0

        calc = strap_pricing.compute_strap_line(g.db, product_line, product,
                                                  discount_pct=discount_pct, credit_term=credit_term,
                                                  hidden_markup_pct=hidden_markup_pct)
        calc_full = strap_pricing.compute_strap_line(g.db, product_line, product,
                                                       discount_pct=0, credit_term=credit_term,
                                                       hidden_markup_pct=hidden_markup_pct)
        total_kg = cost_engine.round_half_up(calc["gross_weight_kg"] * qty_coils, 2)
        unit_price = calc["cfr_price_kg"]
        unit_price_full = calc_full["cfr_price_kg"]
        gross = cost_engine.round_half_up(unit_price * total_kg, 2)
        gross_full = cost_engine.round_half_up(unit_price_full * total_kg, 2)
        return jsonify({
            "unit_price_usd_kg": unit_price,
            "unit_price_full_usd_kg": unit_price_full,
            "total_kg": total_kg,
            "line_gross": gross,
            "line_gross_full": gross_full,
            "gross_weight_kg": calc["gross_weight_kg"],
            "net_weight_kg": calc["net_weight_kg"],
            "ex_work_price_roll": calc["ex_work_price_roll"],
            "fob_price_roll": calc["fob_price_roll"],
            "cfr_price_roll": calc["cfr_price_roll"],
            "ex_work_price_kg": calc["ex_work_price_kg"],
            "fob_price_kg": calc["fob_price_kg"],
            "cfr_price_kg": calc["cfr_price_kg"],
            "meter_weight_g_per_m": calc["meter_weight_g_per_m"],
            "meters_per_coil": product["meters_per_coil"],
            "core_weight_kg": product["core_weight_kg"],
            "suggested_rolls_per_pallet": strap_pricing.suggest_rolls_per_pallet(
                product["core_weight_kg"], bool(product["has_box"])),
            "suggested_pallets_per_container": strap_pricing.suggest_pallets_per_container(
                product["core_weight_kg"], bool(product["has_box"]),
                bool(product["ctr20"]), bool(product["ctr40"])),
        })

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
        creator_seller_type = (creator["seller_type"] if creator and "seller_type" in creator.keys() else None)
        creator_strap_markup_pct = (creator["strap_markup_pct"] if creator and "strap_markup_pct" in creator.keys()
                                     else 0) or 0

        for l in data.get("lines", []):
            line_product_line = l.get("product_line") or "stretch_film"

            if line_product_line in ("pet", "pp"):
                is_custom = bool(l.get("strap_custom"))
                if is_custom:
                    strap_product, err_resp, err_code = _build_custom_strap_product(l, line_product_line)
                    if strap_product is None:
                        continue
                    strap_product_id = None
                else:
                    strap_product = db.execute(
                        "SELECT * FROM strap_product WHERE id=? AND line_key=?",
                        (l.get("strap_product_id"), line_product_line),
                    ).fetchone()
                    if not strap_product:
                        continue
                    strap_product_id = strap_product["id"]
                # v33 -- quantity is now entered as Qty (pallets) x an
                # editable Rolls/pallet (like Stretch Film), not typed
                # directly as a coil count. The client computes the coil
                # count (quantity_coils = pallets x rolls/pallet) for the
                # pricing math; the pallets count itself is what's stored
                # in quantity_pallets for display, same column Stretch
                # Film lines already use for their own pallet count.
                qty_coils = float(l.get("quantity_coils") or 0)
                qty_pallets_display = float(l.get("quantity_pallets") or 0)
                line_discount_pct = float(l.get("line_discount_pct") or 0)
                discount_pct = line_discount_pct + global_discount_pct
                credit_term = _strap_credit_term(data)
                calc = strap_pricing.compute_strap_line(db, line_product_line, strap_product,
                                                          discount_pct=discount_pct, credit_term=credit_term,
                                                          hidden_markup_pct=creator_strap_markup_pct)
                calc_full = strap_pricing.compute_strap_line(db, line_product_line, strap_product,
                                                               discount_pct=0, credit_term=credit_term,
                                                               hidden_markup_pct=creator_strap_markup_pct)
                total_kg = cost_engine.round_half_up(calc["gross_weight_kg"] * qty_coils, 2)
                unit_price = calc["cfr_price_kg"]
                unit_price_full = calc_full["cfr_price_kg"]
                if is_custom:
                    db.execute(
                        """INSERT INTO quotation_line
                           (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                            unit_price_usd_kg, unit_price_full_usd_kg, total_kg, line_discount_pct,
                            pricing_basis, product_line, strap_custom_bom_key, strap_custom_width_mm,
                            strap_custom_thickness_mm, strap_custom_meters_per_coil,
                            strap_custom_core_weight_kg, strap_custom_has_box, strap_custom_ctr20,
                            strap_custom_ctr40)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (quotation_id, None, "Credit" if credit_term else "Cash", "Per Coil",
                         qty_pallets_display, unit_price, unit_price_full, total_kg, line_discount_pct,
                         "per_coil", line_product_line, strap_product["bom_key"], strap_product["width_mm"],
                         strap_product["thickness_mm"], strap_product["meters_per_coil"],
                         strap_product["core_weight_kg"], int(strap_product["has_box"]),
                         int(strap_product["ctr20"]), int(strap_product["ctr40"])),
                    )
                else:
                    db.execute(
                        """INSERT INTO quotation_line
                           (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                            unit_price_usd_kg, unit_price_full_usd_kg, total_kg, line_discount_pct,
                            pricing_basis, product_line)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (quotation_id, strap_product_id, "Credit" if credit_term else "Cash", "Per Coil",
                         qty_pallets_display, unit_price, unit_price_full, total_kg, line_discount_pct,
                         "per_coil", line_product_line),
                    )
                continue

            product = db.execute("SELECT * FROM product WHERE id=?", (l.get("product_id"),)).fetchone()
            if not product:
                continue
            pallet_type = l.get("pallet_type", "Standard Pallet")
            pricing_basis = l.get("pricing_basis", "per_kg")

            colored = bool(l.get("colored"))
            # v27: combine this line's own Discount % with the quotation's
            # Global Discount % (percentage points) -- both come off the
            # margin factor the same way, see pricing._discounted_factor().
            line_discount_pct = float(l.get("line_discount_pct") or 0)
            discount_pct = line_discount_pct + global_discount_pct

            if is_prestretch(product):
                roll_weight_kg = float(l.get("prestretch_roll_weight_kg") or 0)
                core_weight_kg = float(l.get("prestretch_core_weight_kg") or 0)
                rolls_per_pallet = float(l.get("prestretch_rolls_per_pallet") or 0)
                packaging_type = l.get("prestretch_packaging_type", "no_boxes")
                unit_price, total_kg = compute_prestretch_line(
                    db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0),
                    roll_weight_kg, core_weight_kg, rolls_per_pallet, packaging_type,
                    price_adjustment_usd_kg=adjustment, pricing_basis=pricing_basis,
                    seller_type=creator_seller_type, colored=colored, discount_pct=discount_pct,
                )
                unit_price_full, _ = compute_prestretch_line(
                    db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0),
                    roll_weight_kg, core_weight_kg, rolls_per_pallet, packaging_type,
                    price_adjustment_usd_kg=adjustment, pricing_basis=pricing_basis,
                    seller_type=creator_seller_type, colored=colored, discount_pct=0,
                )
                db.execute(
                    """INSERT INTO quotation_line
                       (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                        unit_price_usd_kg, unit_price_full_usd_kg, total_kg, line_discount_pct, pricing_basis,
                        colored, prestretch_roll_weight_kg, prestretch_core_weight_kg,
                        prestretch_rolls_per_pallet, prestretch_packaging_type)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (quotation_id, product["id"], pallet_type, l.get("packing_type", "Automatic"),
                     float(l.get("quantity_pallets") or 0), unit_price, unit_price_full, total_kg,
                     line_discount_pct, pricing_basis, int(colored),
                     roll_weight_kg, core_weight_kg, rolls_per_pallet, packaging_type),
                )
                continue

            custom_roll_weight_kg = l.get("custom_roll_weight_kg")
            custom_core_weight_kg = l.get("custom_core_weight_kg")
            custom_width_mm = l.get("custom_width_mm")
            custom_rolls_per_pallet = l.get("custom_rolls_per_pallet")
            # v21.1: the line's own Packing type selection (Automatic /
            # Manual(5kg) / Manual(2.3~3.5kg) / Manual(2.2kg) / Manual(1.5kg))
            # now actually drives the price (margin + packaging cost), not
            # just the product's own catalog Automatic/Manual value -- see
            # cost_engine.with_overrides()'s auto_manual param.
            auto_manual_override = l.get("packing_type") or None
            # v39 -- UV checkbox (see api_calculate_line's matching comment).
            uv_type = cost_engine.uv_type_for_product(product["stretch_ability"]) if l.get("uv") else None
            unit_price, total_kg = compute_line(
                db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0),
                price_adjustment_usd_kg=adjustment, pallet_type=pallet_type, pricing_basis=pricing_basis,
                roll_weight_kg=custom_roll_weight_kg, core_weight_kg=custom_core_weight_kg,
                width_mm=custom_width_mm, rolls_per_pallet_override=custom_rolls_per_pallet,
                seller_type=creator_seller_type, auto_manual_override=auto_manual_override, colored=colored,
                discount_pct=discount_pct, uv_type=uv_type,
            )
            unit_price_full, _ = compute_line(
                db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0),
                price_adjustment_usd_kg=adjustment, pallet_type=pallet_type, pricing_basis=pricing_basis,
                roll_weight_kg=custom_roll_weight_kg, core_weight_kg=custom_core_weight_kg,
                width_mm=custom_width_mm, rolls_per_pallet_override=custom_rolls_per_pallet,
                seller_type=creator_seller_type, auto_manual_override=auto_manual_override, colored=colored,
                discount_pct=0, uv_type=uv_type,
            )
            db.execute(
                """INSERT INTO quotation_line
                   (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                    unit_price_usd_kg, unit_price_full_usd_kg, total_kg, line_discount_pct, pricing_basis, colored,
                    custom_roll_weight_kg, custom_core_weight_kg, custom_width_mm, custom_rolls_per_pallet, uv_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (quotation_id, product["id"], pallet_type,
                 l.get("packing_type", "Automatic"), float(l.get("quantity_pallets") or 0),
                 unit_price, unit_price_full, total_kg, line_discount_pct, pricing_basis, int(colored),
                 (float(custom_roll_weight_kg) if custom_roll_weight_kg not in (None, "") else None),
                 (float(custom_core_weight_kg) if custom_core_weight_kg not in (None, "") else None),
                 (float(custom_width_mm) if custom_width_mm not in (None, "") else None),
                 (float(custom_rolls_per_pallet) if custom_rolls_per_pallet not in (None, "") else None),
                 uv_type),
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

    @app.route("/quotations/<int:qid>/excel")
    @login_required
    def quotation_excel(qid):
        q, lines, totals = load_quotation(g.db, qid)
        buf = build_xlsx(q, lines, totals)
        filename = f"Quotation_{q['quotation_no'] or q['id']}.xlsx"
        return send_file(
            buf, as_attachment=True, download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def load_quotation(db, qid):
        q = db.execute("SELECT * FROM quotation WHERE id=?", (qid,)).fetchone()
        if not q:
            abort(404)
        if g.user["role"] != "admin" and q["created_by_id"] != g.user["id"]:
            abort(403)
        line_rows = db.execute(
            """SELECT ql.*, p.stretch_ability, p.micron, sp.code AS strap_code
               FROM quotation_line ql
               LEFT JOIN product p ON p.id = ql.product_id
                    AND (ql.product_line IS NULL OR ql.product_line = 'stretch_film')
               LEFT JOIN strap_product sp ON sp.id = ql.product_id
                    AND ql.product_line IN ('pet', 'pp')
               WHERE ql.quotation_id=?""",
            (qid,),
        ).fetchall()
        basis_labels = {"gross": "$/Roll (Gross)", "net": "$/Roll (Net)", "per_kg": "$/KG",
                         "per_coil": "$/KG (CFR)"}
        lines = []
        for l in line_rows:
            line_pl = l["product_line"] if "product_line" in l.keys() and l["product_line"] else "stretch_film"
            if line_pl in ("pet", "pp"):
                if l["strap_code"]:
                    label = f"{strap_pricing.LINE_CONFIG[line_pl]['label']} – {l['strap_code']}"
                elif "strap_custom_width_mm" in l.keys() and l["strap_custom_width_mm"]:
                    bom_labels = dict(strap_pricing.BOM_LABELS.get(line_pl, []))
                    bom_label = bom_labels.get(l["strap_custom_bom_key"], l["strap_custom_bom_key"])
                    label = (f"{strap_pricing.LINE_CONFIG[line_pl]['label']} – Custom "
                             f"{l['strap_custom_width_mm']:g}x{l['strap_custom_thickness_mm']:g}mm ({bom_label})")
                else:
                    label = "-"
            else:
                label = f"{l['micron']}μm – {l['stretch_ability']}" if l["stretch_ability"] else "-"
                # v36 -- UV additive: shown on the label so the saved
                # quotation/PDF makes clear this line carries the UV %,
                # even though it's still priced off the same base product.
                uv_type_val = l["uv_type"] if "uv_type" in l.keys() else None
                if uv_type_val:
                    uv_labels = dict(cost_engine.UV_TYPES)
                    label += f" + UV ({uv_labels.get(uv_type_val, uv_type_val)})"
            # v27: unit_price_usd_kg already has the discount baked in (it
            # comes off the margin factor at save time, not applied again
            # here) -- so the line total is a plain multiply, no further
            # discount math. unit_price_full_usd_kg is the frozen no-discount
            # reference price, used only to show the Discount $ figure in
            # compute_totals(); NULL on quotes saved before v27 falls back
            # to unit_price_usd_kg (i.e. shows as no discount).
            line_total = cost_engine.round_half_up(l["unit_price_usd_kg"] * l["total_kg"], 2)
            full_unit = l["unit_price_full_usd_kg"] if ("unit_price_full_usd_kg" in l.keys()
                                                          and l["unit_price_full_usd_kg"] is not None) else l["unit_price_usd_kg"]
            line_total_full = cost_engine.round_half_up(full_unit * l["total_kg"], 2)
            basis = l["pricing_basis"] if "pricing_basis" in l.keys() and l["pricing_basis"] else "per_kg"
            # v39 -- "stuffing" details (Rolls/Pallet, Pallets/Container --
            # how the rolls actually get loaded/stuffed into the chosen
            # container) are computed here from the line's own saved
            # core-weight/box/container-type inputs and shown on the
            # exported PDF/Excel, right under each strap line, rather than
            # being a separate manual data-entry field anywhere.
            stuffing = None
            if line_pl in ("pet", "pp") and "strap_custom_core_weight_kg" in l.keys() and l["strap_custom_core_weight_kg"]:
                core_weight_kg = l["strap_custom_core_weight_kg"]
                has_box = bool(l["strap_custom_has_box"]) if "strap_custom_has_box" in l.keys() else False
                ctr20 = bool(l["strap_custom_ctr20"]) if "strap_custom_ctr20" in l.keys() else False
                ctr40 = bool(l["strap_custom_ctr40"]) if "strap_custom_ctr40" in l.keys() else False
                if not (ctr20 or ctr40):
                    ctr40 = True
                stuffing = {
                    "rolls_per_pallet": strap_pricing.suggest_rolls_per_pallet(core_weight_kg, has_box),
                    "pallets_per_container": strap_pricing.suggest_pallets_per_container(
                        core_weight_kg, has_box, ctr20, ctr40),
                    "box": "Yes" if has_box else "No",
                    "container": "20ft" if ctr20 else "40ft",
                }
            lines.append(dict(l, label=label, line_total=line_total, line_total_full=line_total_full,
                               pricing_basis_label=basis_labels.get(basis, "$/KG"), stuffing=stuffing))
        totals = compute_totals(db, q, lines)
        return q, lines, totals

    def _fob_addon_for_port(db, port_name):
        row = db.execute("SELECT fob_addon_usd FROM loading_port WHERE port=?", (port_name,)).fetchone()
        return (row["fob_addon_usd"] if row else 0) or 0

    def _freight_for_destination(db, destination):
        row = db.execute("SELECT shipping_rate_usd FROM freight WHERE country=?", (destination,)).fetchone()
        if not row or not row["shipping_rate_usd"]:
            return 0.0
        # shipping_rate_usd is free-text (an admin may enter "550-600"); pull
        # the first number out of it rather than crashing on a non-numeric
        # string, and use that as the CIF freight add-on.
        m = re.search(r"[\d.]+", str(row["shipping_rate_usd"]))
        return float(m.group(0)) if m else 0.0

    def compute_totals(db, q, lines=None):
        # v27: discount now comes off each line's own margin factor (see
        # pricing._discounted_factor()) and is already baked into the
        # stored/computed unit_price_usd_kg -- so `total` here is simply the
        # sum of the (already net) line totals, not a further % reduction.
        # `subtotal` is the pre-discount REFERENCE total (built from each
        # line's frozen unit_price_full_usd_kg), kept only so the "Subtotal
        # (EX-Work)" / "Discount" rows in the quote builder, saved-quotation
        # view and PDF keep meaning what their labels say, with no other
        # display-layer changes needed.
        if lines is None:
            line_rows = db.execute("SELECT * FROM quotation_line WHERE quotation_id=?", (q["id"],)).fetchall()
            lines = []
            for l in line_rows:
                full_unit = l["unit_price_full_usd_kg"] if ("unit_price_full_usd_kg" in l.keys()
                                                              and l["unit_price_full_usd_kg"] is not None) else l["unit_price_usd_kg"]
                lines.append({
                    "line_total": cost_engine.round_half_up(l["unit_price_usd_kg"] * l["total_kg"], 2),
                    "line_total_full": cost_engine.round_half_up(full_unit * l["total_kg"], 2),
                })
        total = cost_engine.round_half_up(sum(l["line_total"] for l in lines), 2)
        subtotal = cost_engine.round_half_up(sum(l.get("line_total_full", l["line_total"]) for l in lines), 2)

        # FOB Total = EX-Work total (after discount) + the selected loading
        # port's flat handling/customs/trucking add-on (Alexandria vs
        # Damietta -- editable in Admin > Loading Ports, since these rates
        # move). CIF Total = FOB Total + freight to the selected
        # destination (from the existing Freight table). Both are shown to
        # the client as the FOB and CIF offers side by side; the raw
        # freight $ figure itself is not broken out as its own line, same
        # treatment as the hidden foreign-seller markup.
        fob_addon = _fob_addon_for_port(db, q["loading_port"] if "loading_port" in q.keys() else None)
        fob_total = cost_engine.round_half_up(total + fob_addon, 2)
        freight_amt = _freight_for_destination(db, q["destination"] if "destination" in q.keys() else None)
        cif_total = cost_engine.round_half_up(fob_total + freight_amt, 2)

        return {"subtotal": subtotal, "total": total, "fob_total": fob_total, "cif_total": cif_total}

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
               seller_type=?, strap_markup_pct=? WHERE id=?""",
            (
                request.form.get("full_name", "").strip(),
                request.form.get("role", "sales_rep"),
                request.form.get("region", "").strip(),
                1 if request.form.get("active") == "on" else 0,
                float(request.form.get("price_adjustment_usd_kg") or 0),
                request.form.get("seller_type", "local"),
                float(request.form.get("strap_markup_pct") or 0),
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
                       core_weight_kg=?, width_mm=?, packaging_group=?, ex_work_usd_kg=?, fob_usd_kg=?,
                       cfr_usd_kg=? WHERE id=?""",
                    (
                        request.form.get("stretch_ability", "").strip(),
                        request.form.get("micron", "").strip(),
                        float(request.form.get("rolls_per_pallet") or 0),
                        float(request.form.get("roll_weight_kg") or 0),
                        float(request.form.get("core_weight_kg") or 0),
                        float(request.form.get("width_mm") or 0) or None,
                        request.form.get("packaging_group", "").strip() or None,
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

    @app.route("/admin/cost/margin-factors", methods=["GET", "POST"])
    @admin_required
    def admin_margin_factors():
        db = g.db
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                db.execute(
                    """INSERT INTO margin_factor
                       (film_type, micron_min, micron_max, packing_type, roll_size, margin_pct)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        request.form.get("film_type", "").strip(),
                        float(request.form.get("micron_min") or 0),
                        float(request.form.get("micron_max") or 0),
                        request.form.get("packing_type", "").strip(),
                        request.form.get("roll_size", "").strip(),
                        float(request.form.get("margin_pct") or 0),
                    ),
                )
                db.commit()
                flash("Margin factor added.", "success")
            else:
                mid = request.form.get("margin_factor_id")
                db.execute(
                    """UPDATE margin_factor SET film_type=?, micron_min=?, micron_max=?, packing_type=?,
                       roll_size=?, margin_pct=? WHERE id=?""",
                    (
                        request.form.get("film_type", "").strip(),
                        float(request.form.get("micron_min") or 0),
                        float(request.form.get("micron_max") or 0),
                        request.form.get("packing_type", "").strip(),
                        request.form.get("roll_size", "").strip(),
                        float(request.form.get("margin_pct") or 0),
                        mid,
                    ),
                )
                db.commit()
                flash("Margin factor updated.", "success")
            return redirect(url_for("admin_margin_factors"))
        rows = db.execute(
            "SELECT * FROM margin_factor ORDER BY film_type, packing_type, micron_min"
        ).fetchall()
        last_upload = table_sync.get_last_upload(db, "margin_factor")
        return render_template("admin_margin_factors.html", rows=rows, last_upload=last_upload)

    @app.route("/admin/factors", methods=["GET", "POST"])
    @admin_required
    def admin_factors():
        # v19: the old Country/Customer Classification margin screen is
        # retired -- margin now comes entirely from Margin Factors (Micron x
        # Film type x Automatic/Manual x Roll size). The `factor` table and
        # its columns on `quotation` are left in the schema (harmless dead
        # data, per the owner's instruction not to touch a live table's
        # schema) but this page no longer renders at all, so there is no
        # stray classification UI left to stumble onto.
        return redirect(url_for("admin_margin_factors"))

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

    @app.route("/admin/loading-ports", methods=["GET", "POST"])
    @admin_required
    def admin_loading_ports():
        db = g.db
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                port = request.form.get("port", "").strip()
                addon = request.form.get("fob_addon_usd") or 0
                if port:
                    db.execute("INSERT INTO loading_port (port, fob_addon_usd) VALUES (?, ?)",
                               (port, float(addon)))
                    db.commit()
                    flash("Loading port added.", "success")
            else:
                pid = request.form.get("port_id")
                db.execute(
                    "UPDATE loading_port SET port=?, fob_addon_usd=? WHERE id=?",
                    (request.form.get("port", "").strip(), float(request.form.get("fob_addon_usd") or 0), pid),
                )
                db.commit()
                flash("Loading port updated.", "success")
            return redirect(url_for("admin_loading_ports"))
        loading_ports = db.execute("SELECT * FROM loading_port ORDER BY port").fetchall()
        return render_template("admin_loading_ports.html", loading_ports=loading_ports)

    @app.route("/admin/loading-ports/<int:pid>/delete", methods=["POST"])
    @admin_required
    def admin_loading_port_delete(pid):
        g.db.execute("DELETE FROM loading_port WHERE id=?", (pid,))
        g.db.commit()
        flash("Loading port deleted.", "success")
        return redirect(url_for("admin_loading_ports"))

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
        # v34 -- the 3 strap_-prefixed freight/credit settings now live on
        # the dedicated Strap Costing page instead (dollar_rate stays here
        # too since it's shared by Stretch Film and Strap alike).
        if request.method == "POST":
            for row in db.execute("SELECT key FROM global_setting WHERE key NOT LIKE 'strap_%'").fetchall():
                key = row["key"]
                val = request.form.get(f"value_{key}")
                if val is not None and val != "":
                    db.execute("UPDATE global_setting SET value=? WHERE key=?", (float(val), key))
            db.commit()
            flash("Global cost settings updated.", "success")
            return redirect(url_for("admin_global_settings"))
        settings = db.execute(
            "SELECT * FROM global_setting WHERE key NOT LIKE 'strap_%' ORDER BY label"
        ).fetchall()
        last_upload = table_sync.get_last_upload(db, "global_setting")
        return render_template("admin_global_settings.html", settings=settings, last_upload=last_upload)

    @app.route("/admin/cost/materials", methods=["GET", "POST"])
    @admin_required
    def admin_material_rates():
        db = g.db
        # v34 -- PET/PP Strap's own materials (pet_*/pp_* keys) now live on
        # the dedicated Strap Costing page instead, so they're excluded here.
        if request.method == "POST":
            for row in db.execute(
                "SELECT id FROM material_rate WHERE material_key NOT LIKE 'pet_%' AND material_key NOT LIKE 'pp_%'"
            ).fetchall():
                val = request.form.get(f"value_{row['id']}")
                if val is not None and val != "":
                    db.execute("UPDATE material_rate SET value=? WHERE id=?", (float(val), row["id"]))
            db.commit()
            flash("Material rates updated.", "success")
            return redirect(url_for("admin_material_rates"))
        resin = db.execute(
            "SELECT * FROM material_rate WHERE category='resin' AND material_key NOT LIKE 'pet_%' "
            "AND material_key NOT LIKE 'pp_%' ORDER BY label"
        ).fetchall()
        packaging = db.execute(
            "SELECT * FROM material_rate WHERE category='packaging' AND material_key NOT LIKE 'pet_%' "
            "AND material_key NOT LIKE 'pp_%' ORDER BY label"
        ).fetchall()
        last_upload = table_sync.get_last_upload(db, "material_rate")
        return render_template("admin_material_rates.html", resin=resin, packaging=packaging,
                                last_upload=last_upload)

    @app.route("/admin/strap-costing", methods=["GET", "POST"])
    @admin_required
    def admin_strap_costing():
        """v34 -- single consolidated page for everything that prices the
        PET Strap / PP Strap lines: dollar rate (shared with Stretch Film),
        their own material prices, BOM recipes (composition/profit/waste),
        per-line fixed cost/electricity/direct labor, and the freight (FOB/
        shipping per container) + credit-term surcharge settings."""
        db = g.db
        if request.method == "POST":
            # Dollar rate (shared global_setting -- also editable from
            # Global Settings; same underlying row, so either page's edit
            # takes effect for both Stretch Film and Strap).
            val = request.form.get("dollar_rate")
            if val is not None and val != "":
                db.execute("UPDATE global_setting SET value=? WHERE key='dollar_rate'", (float(val),))

            # Material prices (pet_*/pp_* rows only).
            for row in db.execute(
                "SELECT id FROM material_rate WHERE material_key LIKE 'pet_%' OR material_key LIKE 'pp_%'"
            ).fetchall():
                val = request.form.get(f"value_{row['id']}")
                if val is not None and val != "":
                    db.execute("UPDATE material_rate SET value=? WHERE id=?", (float(val), row["id"]))

            # BOM recipes: profit %, waste %, and each component's fraction.
            for row in db.execute("SELECT id, components_json FROM strap_bom").fetchall():
                profit_val = request.form.get(f"bom_{row['id']}_profit")
                waste_val = request.form.get(f"bom_{row['id']}_waste")
                if profit_val is not None and profit_val != "":
                    db.execute("UPDATE strap_bom SET profit_pct=? WHERE id=?",
                               (float(profit_val) / 100.0, row["id"]))
                if waste_val is not None and waste_val != "":
                    db.execute("UPDATE strap_bom SET waste_pct=? WHERE id=?",
                               (float(waste_val) / 100.0, row["id"]))
                components = json.loads(row["components_json"])
                changed = False
                for comp_key in list(components.keys()):
                    comp_val = request.form.get(f"bom_{row['id']}_comp_{comp_key}")
                    if comp_val is not None and comp_val != "":
                        components[comp_key] = float(comp_val) / 100.0
                        changed = True
                if changed:
                    db.execute("UPDATE strap_bom SET components_json=? WHERE id=?",
                               (json.dumps(components), row["id"]))

            # Per-line electricity / fixed cost / direct labor.
            for line_key in ("pet", "pp"):
                elec_val = request.form.get(f"linecfg_{line_key}_electricity")
                fixed_val = request.form.get(f"linecfg_{line_key}_fixed_cost")
                labor_val = request.form.get(f"linecfg_{line_key}_direct_labor")
                if elec_val is not None and elec_val != "":
                    db.execute("UPDATE strap_line_config SET electricity_per_ton_egp=? WHERE line_key=?",
                               (float(elec_val), line_key))
                if fixed_val is not None and fixed_val != "":
                    db.execute("UPDATE strap_line_config SET fixed_cost_per_kg_usd=? WHERE line_key=?",
                               (float(fixed_val), line_key))
                if labor_val is not None and labor_val != "":
                    db.execute("UPDATE strap_line_config SET direct_labor_per_kg_usd=? WHERE line_key=?",
                               (float(labor_val), line_key))

            # Freight / credit-term settings (strap_-prefixed global_setting rows).
            for row in db.execute("SELECT key FROM global_setting WHERE key LIKE 'strap_%'").fetchall():
                key = row["key"]
                val = request.form.get(f"value_{key}")
                if val is not None and val != "":
                    db.execute("UPDATE global_setting SET value=? WHERE key=?", (float(val), key))

            db.commit()
            flash("PET/PP Strap costing updated.", "success")
            return redirect(url_for("admin_strap_costing"))

        dollar_rate = db.execute("SELECT value FROM global_setting WHERE key='dollar_rate'").fetchone()
        material_rates = {
            "pet_resin": db.execute(
                "SELECT * FROM material_rate WHERE material_key LIKE 'pet_%' AND category='resin' ORDER BY label"
            ).fetchall(),
            "pet_packaging": db.execute(
                "SELECT * FROM material_rate WHERE material_key LIKE 'pet_%' AND category='packaging' ORDER BY label"
            ).fetchall(),
            "pp_resin": db.execute(
                "SELECT * FROM material_rate WHERE material_key LIKE 'pp_%' AND category='resin' ORDER BY label"
            ).fetchall(),
            "pp_packaging": db.execute(
                "SELECT * FROM material_rate WHERE material_key LIKE 'pp_%' AND category='packaging' ORDER BY label"
            ).fetchall(),
        }
        boms = {}
        for line_key in ("pet", "pp"):
            rows = db.execute(
                "SELECT * FROM strap_bom WHERE line_key=?", (line_key,)
            ).fetchall()
            label_map = dict(strap_pricing.BOM_LABELS.get(line_key, []))
            entries = []
            for r in rows:
                entries.append({
                    "id": r["id"],
                    "bom_key": r["bom_key"],
                    "label": label_map.get(r["bom_key"], r["bom_key"]),
                    "profit_pct": r["profit_pct"] * 100.0,
                    "waste_pct": r["waste_pct"] * 100.0,
                    "components": [
                        (k, v * 100.0) for k, v in json.loads(r["components_json"]).items()
                    ],
                })
            # keep the same display order as BOM_LABELS
            order = [k for k, _ in strap_pricing.BOM_LABELS.get(line_key, [])]
            entries.sort(key=lambda e: order.index(e["bom_key"]) if e["bom_key"] in order else 99)
            boms[line_key] = entries
        line_configs = {
            r["line_key"]: r
            for r in db.execute("SELECT * FROM strap_line_config").fetchall()
        }
        freight = db.execute(
            "SELECT * FROM global_setting WHERE key LIKE 'strap_%' ORDER BY label"
        ).fetchall()
        return render_template(
            "admin_strap_costing.html",
            dollar_rate=dollar_rate["value"] if dollar_rate else 45,
            material_rates=material_rates,
            boms=boms,
            line_configs=line_configs,
            freight=freight,
        )

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
        last_upload = table_sync.get_last_upload(db, "labor_employee")
        return render_template("admin_labor.html", employees=employees,
                                fixed_total=round(fixed_total, 2), variable_total=round(variable_total, 2),
                                last_upload=last_upload)

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
        last_upload_power = table_sync.get_last_upload(db, "electricity_power")
        last_upload_capacity = table_sync.get_last_upload(db, "production_capacity")
        return render_template("admin_electricity.html", power=power, capacity=capacity,
                                last_upload_power=last_upload_power, last_upload_capacity=last_upload_capacity)

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
        last_upload = table_sync.get_last_upload(db, "variable_cost_item")
        return render_template("admin_variable_costs.html", items=items, last_upload=last_upload)

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
        last_upload = table_sync.get_last_upload(db, "fixed_cost_item")
        return render_template("admin_fixed_costs.html", by_category=by_category, total=round(total, 2),
                                last_upload=last_upload)

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
                   stretch_kg=?, box_qty=?, rolls_per_box=?, cartoon_angle_qty=?, scotch_tape_qty=?,
                   air_bag_qty=?, pe_bag_qty=? WHERE id=?""",
                (
                    float(request.form.get("pallet_qty") or 0), float(request.form.get("cardboard_qty") or 0),
                    float(request.form.get("cap_qty") or 0), float(request.form.get("corrugated_kg") or 0),
                    float(request.form.get("stretch_kg") or 0), float(request.form.get("box_qty") or 0),
                    float(request.form.get("rolls_per_box") or 0), float(request.form.get("cartoon_angle_qty") or 0),
                    float(request.form.get("scotch_tape_qty") or 0),
                    float(request.form.get("air_bag_qty") or 0), float(request.form.get("pe_bag_qty") or 0), pid,
                ),
            )
            db.commit()
            flash("Pallet / packaging component updated.", "success")
            return redirect(url_for("admin_pallet"))
        rows = db.execute("SELECT * FROM pallet_component ORDER BY label").fetchall()
        totals = {r["packing_key"]: round(cost_engine.pallet_component_total_usd(db, r["packing_key"]), 4)
                   for r in rows}
        last_upload = table_sync.get_last_upload(db, "pallet_component")
        return render_template("admin_pallet.html", rows=rows, totals=totals, last_upload=last_upload)

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
        last_upload = table_sync.get_last_upload(db, "bom_row")
        return render_template("admin_bom.html", rows=rows, last_upload=last_upload)

    @app.route("/admin/cost/prestretch", methods=["GET", "POST"])
    @admin_required
    def admin_prestretch():
        db = g.db
        if request.method == "POST":
            pid = request.form.get("product_id")
            source_id = request.form.get("prestretch_source_product_id") or None
            db.execute(
                "UPDATE product SET prestretch_source_product_id=? WHERE id=?", (source_id, pid)
            )
            db.commit()
            flash("Pre-Stretch source mapping updated.", "success")
            return redirect(url_for("admin_prestretch"))
        rows = db.execute(
            "SELECT * FROM product WHERE is_prestretch=1 ORDER BY CAST(micron AS REAL)"
        ).fetchall()
        # Candidate source products: any non-Pre-Stretch jumbo (50kg) SKU --
        # the only ones the workbook's Pre-Stretch rows ever borrow a sales
        # price from.
        sources = db.execute(
            "SELECT * FROM product WHERE is_prestretch=0 AND roll_weight_kg=50 "
            "ORDER BY stretch_ability, CAST(micron AS REAL)"
        ).fetchall()
        packaging_settings = db.execute(
            "SELECT * FROM global_setting WHERE key IN "
            "('prestretch_packaging_noboxes_usd','prestretch_packaging_boxes_usd') ORDER BY key"
        ).fetchall()
        return render_template(
            "admin_prestretch.html", rows=rows, sources=sources, packaging_settings=packaging_settings,
            product_label=product_label,
        )

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
        last_upload = table_sync.get_last_upload(db, "packing_tier")
        return render_template("admin_packing_tiers.html", rows=rows, last_upload=last_upload)

    # ---------- Admin: per-table Excel round-trip (download/upload/review) ----------
    @app.route("/admin/table-sync/<table_key>/download")
    @admin_required
    def table_sync_download(table_key):
        if table_key not in table_sync.TABLE_CONFIGS:
            abort(404)
        buf = table_sync.export_table_excel(g.db, table_key)
        filename = f"{table_key}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx"
        return send_file(
            buf, as_attachment=True, download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/admin/table-sync/<table_key>/upload", methods=["POST"])
    @admin_required
    def table_sync_upload(table_key):
        if table_key not in table_sync.TABLE_CONFIGS:
            abort(404)
        cfg = table_sync.TABLE_CONFIGS[table_key]
        f = request.files.get("excel_file")
        if not f or not f.filename:
            flash("Choose an .xlsx file first.", "error")
            return redirect(url_for(cfg["redirect_endpoint"]))
        try:
            parsed_rows = table_sync.parse_uploaded_excel(f.stream, table_key)
        except Exception as e:
            flash(f"Could not read that Excel file: {e}", "error")
            return redirect(url_for(cfg["redirect_endpoint"]))
        if not parsed_rows:
            flash("No usable values found in that file (check the yellow 'editable' cells were filled in).",
                  "error")
            return redirect(url_for(cfg["redirect_endpoint"]))
        result = table_sync.diff_table(g.db, table_key, parsed_rows)
        if not result["field_diffs"]:
            flash("No differences found -- every matched value already equals what's in the database.",
                  "success")
            return redirect(url_for(cfg["redirect_endpoint"]))
        payload = json.dumps({"rows": parsed_rows, "filename": f.filename})
        return render_template(
            "admin_table_sync_review.html", table_key=table_key, table_label=cfg["label"],
            field_diffs=result["field_diffs"], price_impact=result["price_impact"],
            filename=f.filename, payload=payload, redirect_endpoint=cfg["redirect_endpoint"],
        )

    @app.route("/admin/table-sync/<table_key>/apply", methods=["POST"])
    @admin_required
    def table_sync_apply(table_key):
        if table_key not in table_sync.TABLE_CONFIGS:
            abort(404)
        cfg = table_sync.TABLE_CONFIGS[table_key]
        try:
            payload = json.loads(request.form.get("payload") or "{}")
            parsed_rows = payload["rows"]
            filename = payload.get("filename", "upload.xlsx")
        except Exception:
            flash("Upload session data was invalid -- please upload the file again.", "error")
            return redirect(url_for(cfg["redirect_endpoint"]))

        # Re-validate/re-diff against the *current* live DB before applying --
        # cheap, and naturally handles the DB having changed since the review
        # page was rendered (e.g. someone hand-edited a value meanwhile).
        result = table_sync.diff_table(g.db, table_key, parsed_rows)
        if not result["field_diffs"]:
            flash("Nothing to apply -- the database already matches (it may have changed since you reviewed).",
                  "success")
            return redirect(url_for(cfg["redirect_endpoint"]))

        field_diffs, price_impact = table_sync.apply_table_changes(
            g.db, table_key, parsed_rows, g.user["username"], filename,
        )
        msg = f"Applied {len(field_diffs)} change(s) from {filename} to {cfg['label']}."
        if price_impact.get("count"):
            msg += (
                f" Estimated final sales price impact: {price_impact['avg_pct']:+.2f}% average "
                f"(range {price_impact['min_pct']:+.2f}% to {price_impact['max_pct']:+.2f}%) "
                f"across {price_impact['count']} product(s)."
            )
        else:
            msg += " No effect on any product's estimated final sales price."
        flash(msg, "success")
        return redirect(url_for(cfg["redirect_endpoint"]))

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
        product_labels = disambiguate_labels(products)
        rows = []
        for p, lbl in zip(products, product_labels):
            bd = cost_engine.breakdown(db, p)
            rows.append({"product": p, "label": lbl, "breakdown": bd})
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
        ["Discount", f"{q['global_discount_pct'] or 0}%", "", ""],
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

    stuffing_style = ParagraphStyle("Stuffing", parent=styles["Normal"], fontSize=7,
                                     textColor=colors.HexColor("#666666"), leading=9, fontName="Helvetica-Oblique")

    header = ["#", "Product", "Pallet", "Packing", "Basis", "Qty (pallets)", "Total KG", "Unit $/KG", "Line Total $"]
    rows = [header]
    span_commands = []
    stuffing_row_indexes = []
    for i, line in enumerate(lines, start=1):
        rows.append([
            str(i), line["label"], line["pallet_type"], line["packing_type"],
            line.get("pricing_basis_label", "$/KG"),
            f"{line['quantity_pallets']:g}", f"{line['total_kg']:,.1f}",
            f"{line['unit_price_usd_kg']:.2f}", f"{line['line_total']:,.2f}",
        ])
        # v39 -- strap lines get a "stuffing" sub-row right under them:
        # Rolls/Pallet and Pallets/Container, computed from the line's own
        # core-weight/box/container inputs (see load_quotation), so the
        # customer-facing export shows exactly how the order gets loaded.
        stuffing = line.get("stuffing")
        if stuffing:
            note = (f"Stuffing — Rolls/Pallet: {stuffing['rolls_per_pallet']} · "
                    f"Pallets/Container ({stuffing['container']}): {stuffing['pallets_per_container']} · "
                    f"Box: {stuffing['box']}")
            row_idx = len(rows)
            rows.append(["", Paragraph(note, stuffing_style), "", "", "", "", "", "", ""])
            span_commands.append(("SPAN", (1, row_idx), (-1, row_idx)))
            stuffing_row_indexes.append(row_idx)
    table = Table(rows, colWidths=[16, 108, 58, 58, 62, 48, 48, 48, 60])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
        *span_commands,
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))

    totals_rows = [
        [f"Global Discount ({q['global_discount_pct'] or 0}%)",
         f"-${cost_engine.round_half_up(totals['subtotal'] - totals['total'], 2):,.2f}"],
        [f"FOB Total ({q['loading_port'] or '-'})", f"${totals['fob_total']:,.2f}"],
        [f"CIF Total ({q['destination'] or '-'})", f"${totals['cif_total']:,.2f}"],
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


def build_xlsx(q, lines, totals):
    """Excel version of the same quotation the PDF builds -- same header
    meta, same line columns, same totals -- as an .xlsx download, requested
    to sit right next to Export PDF."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Quotation"

    header_fill = PatternFill("solid", fgColor="1F2937")
    header_font = Font(color="FFFFFF", bold=True)
    bold = Font(bold=True)
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    right = Alignment(horizontal="right")

    row = 1
    ws.cell(row=row, column=1, value=COMPANY_NAME).font = Font(bold=True, size=13)
    row += 1
    ws.cell(row=row, column=1, value=COMPANY_ADDRESS).font = Font(size=8, color="444444")
    row += 2

    ws.cell(row=row, column=1, value="Quotation").font = Font(bold=True, size=15)
    row += 2

    created_at = q["created_at"] or ""
    date_str = created_at[:10] if created_at else "-"
    meta_rows = [
        ("Quotation No.", q["quotation_no"] or f"#{q['id']}", "Date", date_str),
        ("Customer", q["customer_name"] or "-", "Payment Term", q["payment_term"] or "-"),
        ("Loading Port", q["loading_port"] or "-", "Destination", q["destination"] or "-"),
        ("Discount", f"{q['global_discount_pct'] or 0}%", "", ""),
    ]
    for label1, val1, label2, val2 in meta_rows:
        ws.cell(row=row, column=1, value=label1).font = bold
        ws.cell(row=row, column=2, value=val1)
        if label2:
            ws.cell(row=row, column=3, value=label2).font = bold
            ws.cell(row=row, column=4, value=val2)
        row += 1
    row += 1

    header_row = row
    headers = ["#", "Product", "Pallet", "Packing", "Basis", "Qty (pallets)", "Total KG", "Unit $/KG", "Line Total $"]
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = border
    row += 1

    stuffing_font = Font(italic=True, size=8, color="666666")
    for i, line in enumerate(lines, start=1):
        values = [
            i, line["label"], line["pallet_type"], line["packing_type"],
            line.get("pricing_basis_label", "$/KG"),
            line["quantity_pallets"], cost_engine.round_half_up(line["total_kg"], 1),
            cost_engine.round_half_up(line["unit_price_usd_kg"], 2), cost_engine.round_half_up(line["line_total"], 2),
        ]
        for col, v in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col, value=v)
            cell.border = border
            if col >= 6:
                cell.alignment = right
        row += 1
        # v39 -- strap lines get a "stuffing" note right under them:
        # Rolls/Pallet and Pallets/Container, computed from the line's own
        # core-weight/box/container inputs (see load_quotation).
        stuffing = line.get("stuffing")
        if stuffing:
            note = (f"Stuffing — Rolls/Pallet: {stuffing['rolls_per_pallet']} · "
                    f"Pallets/Container ({stuffing['container']}): {stuffing['pallets_per_container']} · "
                    f"Box: {stuffing['box']}")
            cell = ws.cell(row=row, column=2, value=note)
            cell.font = stuffing_font
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=9)
            row += 1

    row += 1
    totals_rows = [
        (f"Global Discount ({q['global_discount_pct'] or 0}%)", -cost_engine.round_half_up(totals["subtotal"] - totals["total"], 2)),
        (f"FOB Total ({q['loading_port'] or '-'})", cost_engine.round_half_up(totals["fob_total"], 2)),
        (f"CIF Total ({q['destination'] or '-'})", cost_engine.round_half_up(totals["cif_total"], 2)),
    ]
    for label, val in totals_rows:
        ws.cell(row=row, column=1, value=label).font = bold
        cell = ws.cell(row=row, column=9, value=val)
        cell.font = bold
        cell.number_format = "$#,##0.00"
        row += 1

    widths = [4, 30, 12, 12, 12, 12, 10, 10, 12]
    for col, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
