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

    # v59 -- browsers (Safari especially) request GET /favicon.ico directly
    # on some navigations/reloads regardless of the <link rel="icon"> tag
    # in base.html's <head>, and cache whatever that returns (a 404 -> the
    # browser's own default icon) per-path rather than always trusting the
    # page's own <link> tag -- this is why the tab icon showed on the page
    # first loaded after the v58 deploy but "disappeared" (fell back to
    # default) on others. Serving the logo at the conventional root path
    # too makes every page/reload resolve to the same icon.
    @app.route("/favicon.ico")
    def favicon():
        return redirect(url_for("static", filename="logo.png"))

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
        # v99 -- admin-only "Act as [salesperson]" preview (owner request):
        # every other active user, so the admin can pick one from a dropdown
        # and see the exact price (hidden markup/bonus baked in) that rep
        # would get -- see _resolve_pricing_user() in api_calculate_line()/
        # _calculate_strap_line() and api_save_quotation()'s acting_as_user.
        preview_users = []
        if g.user["role"] == "admin":
            preview_users = g.db.execute(
                "SELECT id, username, full_name FROM user WHERE active=1 AND id != ? "
                "ORDER BY full_name, username",
                (g.user["id"],),
            ).fetchall()
        return render_template(
            "pricing.html",
            preview_users=preview_users,
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
        # v56 -- "Price adjustment $/KG" is retired per the owner: rep
        # commission now lives ONLY in the hidden Stretch/Strap markup
        # fields below (percent or cents/KG, baked into the price, never
        # shown to the customer or the rep) -- this used to add a SEPARATE,
        # visible-basis adjustment on top for Stretch/Pre-Stretch only.
        # Always 0 now so it's a no-op; the user.price_adjustment_usd_kg
        # column and DB value are left alone (unused) rather than migrated.
        adjustment = 0
        # v99 -- see _resolve_pricing_user()'s docstring: an admin previewing
        # "Act as [salesperson]" gets that salesperson's own seller_type +
        # hidden markup here instead of their own; everyone else always gets
        # their own g.user, unchanged from before.
        pricing_user = _resolve_pricing_user(data)
        seller_type = pricing_user["seller_type"] if "seller_type" in pricing_user.keys() else None
        # v46 -- hidden per-user markup, independent from Strap's own --
        # see user.stretch_markup_mode/stretch_markup_value and
        # cost_engine.apply_hidden_markup().
        hidden_markup_mode = pricing_user["stretch_markup_mode"] if "stretch_markup_mode" in pricing_user.keys() else None
        hidden_markup_value = (pricing_user["stretch_markup_value"] if "stretch_markup_value" in pricing_user.keys()
                                else 0) or 0
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
        # v47: combined discount is silently capped at the admin-configured
        # max (Admin > Global Cost Settings > "Max Discount allowed") so the
        # margin can never be eroded past what the owner approved. v94 --
        # this is Stretch Film's own cap now; Strap has its own separate
        # one (Admin > PET/PP Strap Costing) -- see cost_engine.capped_discount_pct().
        discount_pct, discount_capped = cost_engine.capped_discount_pct(
            g.db, line_discount_pct, global_discount_pct
        )
        # v79 -- same flat credit-term $/kg surcharge Strap has always had,
        # now applied here too (Extras > Credit payment terms extra) -- see
        # _is_credit_term()/cost_engine.credit_term_extra_usd_kg().
        credit_term = _is_credit_term(data)

        if is_prestretch(product):
            roll_weight_kg = float(data.get("prestretch_roll_weight_kg") or 0)
            core_weight_kg = float(data.get("prestretch_core_weight_kg") or 0)
            rolls_per_pallet = float(data.get("prestretch_rolls_per_pallet") or 0)
            packaging_type = data.get("prestretch_packaging_type", "no_boxes")
            unit_price, total_kg = compute_prestretch_line(
                g.db, product, country_class, customer_class, qty, roll_weight_kg, core_weight_kg,
                rolls_per_pallet, packaging_type, price_adjustment_usd_kg=adjustment, pricing_basis=pricing_basis,
                seller_type=seller_type, colored=colored, discount_pct=discount_pct,
                hidden_markup_mode=hidden_markup_mode, hidden_markup_value=hidden_markup_value,
                credit_term=credit_term,
            )
            unit_price_full, _ = compute_prestretch_line(
                g.db, product, country_class, customer_class, qty, roll_weight_kg, core_weight_kg,
                rolls_per_pallet, packaging_type, price_adjustment_usd_kg=adjustment, pricing_basis=pricing_basis,
                seller_type=seller_type, colored=colored, discount_pct=0,
                hidden_markup_mode=hidden_markup_mode, hidden_markup_value=hidden_markup_value,
                credit_term=credit_term,
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
                "discount_pct_applied": discount_pct,
                "discount_capped": discount_capped,
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
                                             discount_pct=discount_pct, uv_type=uv_type,
                                             hidden_markup_mode=hidden_markup_mode,
                                             hidden_markup_value=hidden_markup_value,
                                             credit_term=credit_term)
        unit_price_full, _ = compute_line(g.db, product, country_class, customer_class, qty,
                                           price_adjustment_usd_kg=adjustment, pallet_type=pallet_type,
                                           pricing_basis=pricing_basis,
                                           roll_weight_kg=custom_roll_weight_kg,
                                           core_weight_kg=custom_core_weight_kg,
                                           width_mm=custom_width_mm,
                                           rolls_per_pallet_override=custom_rolls_per_pallet,
                                           seller_type=seller_type,
                                           auto_manual_override=auto_manual_override, colored=colored,
                                           discount_pct=0, uv_type=uv_type,
                                           hidden_markup_mode=hidden_markup_mode,
                                           hidden_markup_value=hidden_markup_value,
                                           credit_term=credit_term)
        # v70.2 -- raw, unrounded EX-Work price so the client can build FOB
        # the same way the sheet's Stretch!AO does (ROUNDUP on the unrounded
        # base), not on the already-2dp-rounded unit_price.
        unit_price_raw, _ = compute_line(g.db, product, country_class, customer_class, qty,
                                          price_adjustment_usd_kg=adjustment, pallet_type=pallet_type,
                                          pricing_basis=pricing_basis,
                                          roll_weight_kg=custom_roll_weight_kg,
                                          core_weight_kg=custom_core_weight_kg,
                                          width_mm=custom_width_mm,
                                          rolls_per_pallet_override=custom_rolls_per_pallet,
                                          seller_type=seller_type,
                                          auto_manual_override=auto_manual_override, colored=colored,
                                          discount_pct=discount_pct, uv_type=uv_type,
                                          hidden_markup_mode=hidden_markup_mode,
                                          hidden_markup_value=hidden_markup_value,
                                          credit_term=credit_term,
                                          round_result=False)
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
            "unit_price_usd_kg_raw": unit_price_raw,
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
            "discount_pct_applied": discount_pct,
            "discount_capped": discount_capped,
        })

    def _resolve_pricing_user(data):
        """v99 -- admin-only "Act as [salesperson]" preview (owner request):
        an admin can pick another sales rep (e.g. Manuel) from a dropdown at
        the top of the Pricing screen and see the exact final price that rep
        would get -- their own hidden Stretch/Strap markup/bonus baked in --
        without logging out and back in as them. Only an admin account may
        switch: a plain sales rep's own g.user always drives their own
        pricing regardless of what a client sends, so this can't be used to
        see someone else's hidden markup by anything other than an admin
        deliberately choosing to. Falls back to g.user whenever no (or an
        invalid/inactive) preview_as_user_id is sent, so ordinary use is
        unaffected."""
        if g.user["role"] != "admin":
            return g.user
        preview_id = data.get("preview_as_user_id")
        if not preview_id:
            return g.user
        preview_user = g.db.execute(
            "SELECT * FROM user WHERE id=? AND active=1", (preview_id,)
        ).fetchone()
        return preview_user or g.user

    def _is_credit_term(data):
        """The quotation's own Payment Term selector: anything other than
        'Cash (...)' triggers a flat $/kg credit-term surcharge -- PET/PP
        Strap's own (strap_pricing.compute_strap_line()) since v30, and
        (v79) Stretch Film/Pre-Stretch's (pricing.py's
        unit_price_for()/prestretch_unit_price_for(), via
        cost_engine.credit_term_extra_usd_kg()) -- both driven by this same
        one field. Renamed from _strap_credit_term now that it's shared."""
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
        # v47: combined + capped discount rule -- see
        # cost_engine.capped_discount_pct(). v94 -- Strap now has its own
        # Max Discount cap (Admin > PET/PP Strap Costing), separate from
        # Stretch Film's.
        discount_pct, discount_capped = cost_engine.capped_discount_pct(
            g.db, line_discount_pct, global_discount_pct, product_family="strap"
        )
        credit_term = _is_credit_term(data)
        # v46 -- hidden per-user markup (e.g. Manuel/Pasquale), independent
        # from Stretch Film's own -- see user.strap_markup_mode/
        # strap_markup_value and strap_pricing.compute_strap_line().
        # v99 -- resolved through _resolve_pricing_user() so an admin's "Act
        # as [salesperson]" preview picks up that rep's own hidden markup here
        # too, not just on the Stretch Film side.
        pricing_user = _resolve_pricing_user(data)
        hidden_markup_mode = pricing_user["strap_markup_mode"] if "strap_markup_mode" in pricing_user.keys() else None
        hidden_markup_value = (pricing_user["strap_markup_value"] if "strap_markup_value" in pricing_user.keys() else 0) or 0
        # v62 -- only the shipping (freight) leg is shared with Stretch
        # Film's Catalog & Rates > Rates tables now, keyed by the
        # quotation's own Destination pick; FOB stays Strap's own separate
        # flat per-container setting (Admin > PET/PP Strap Costing), same
        # as before v61 -- see strap_pricing.compute_strap_line()'s
        # docstring. Passing fob_container_usd=None makes it fall back to
        # that flat admin setting internally.
        fob_container_usd = None
        shipping_container_usd = _freight_for_destination(g.db, data.get("destination"))

        calc = strap_pricing.compute_strap_line(g.db, product_line, product,
                                                  discount_pct=discount_pct, credit_term=credit_term,
                                                  hidden_markup_mode=hidden_markup_mode,
                                                  hidden_markup_value=hidden_markup_value,
                                                  fob_container_usd=fob_container_usd,
                                                  shipping_container_usd=shipping_container_usd)
        calc_full = strap_pricing.compute_strap_line(g.db, product_line, product,
                                                       discount_pct=0, credit_term=credit_term,
                                                       hidden_markup_mode=hidden_markup_mode,
                                                       hidden_markup_value=hidden_markup_value,
                                                       fob_container_usd=fob_container_usd,
                                                       shipping_container_usd=shipping_container_usd)
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
            # v96 -- max gross-roll-weight alarm (strap_pricing.gross_weight_exceeds_max()):
            # lets the Pricing screen warn live, before the rep even tries to Save.
            "gross_weight_max_kg": calc["gross_weight_max_kg"],
            "gross_weight_exceeded": calc["gross_weight_exceeded"],
            "suggested_rolls_per_pallet": strap_pricing.suggest_rolls_per_pallet(
                product["core_weight_kg"], bool(product["has_box"])),
            "suggested_pallets_per_container": strap_pricing.suggest_pallets_per_container(
                product["core_weight_kg"], bool(product["has_box"]),
                bool(product["ctr20"]), bool(product["ctr40"])),
            "discount_pct_applied": discount_pct,
            "discount_capped": discount_capped,
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

        # v99 -- admin "Act as [salesperson]" preview (see
        # _resolve_pricing_user()'s docstring). For a BRAND-NEW quotation
        # only, an admin previewing as e.g. Manuel gets it saved with
        # created_by_id = Manuel's own id, not the admin's -- that's what
        # makes the markup-resolution below (creator/creator_id, already
        # existing since v46) naturally price it exactly as Manuel would,
        # both now and on every future edit, with no separate "acting as"
        # flag to carry forward. Editing an EXISTING quotation always keeps
        # its original creator (existing["created_by_id"]), same as before
        # v99 -- "Act as" only decides who a brand-new quote is attributed to.
        acting_as_user = None
        if not q_id and g.user["role"] == "admin" and data.get("preview_as_user_id"):
            acting_as_user = db.execute(
                "SELECT * FROM user WHERE id=? AND active=1", (data.get("preview_as_user_id"),)
            ).fetchone()

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
            new_created_by_id = acting_as_user["id"] if acting_as_user else g.user["id"]
            cur = db.execute(
                """INSERT INTO quotation
                   (quotation_no, customer_name, loading_port, destination, payment_term, customer_class,
                    country_class, seller_type, global_discount_pct, status, created_by_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'saved', ?, ?)""",
                (quotation_no, customer_name, loading_port, destination, payment_term, customer_class,
                 country_class, seller_type, global_discount_pct, new_created_by_id,
                 datetime.now(timezone.utc).isoformat()),
            )
            quotation_id = cur.lastrowid

        creator_id = existing["created_by_id"] if q_id else new_created_by_id
        creator = db.execute("SELECT * FROM user WHERE id=?", (creator_id,)).fetchone()
        # v56 -- "Price adjustment $/KG" retired, see the matching note in
        # api_calculate_line() above.
        adjustment = 0
        creator_seller_type = (creator["seller_type"] if creator and "seller_type" in creator.keys() else None)
        # v46 -- hidden per-user markup, independent per product family --
        # see user.stretch_markup_mode/value + strap_markup_mode/value and
        # cost_engine.apply_hidden_markup().
        creator_stretch_markup_mode = (creator["stretch_markup_mode"]
                                        if creator and "stretch_markup_mode" in creator.keys() else None)
        creator_stretch_markup_value = (creator["stretch_markup_value"]
                                         if creator and "stretch_markup_value" in creator.keys() else 0) or 0
        creator_strap_markup_mode = (creator["strap_markup_mode"]
                                      if creator and "strap_markup_mode" in creator.keys() else None)
        creator_strap_markup_value = (creator["strap_markup_value"]
                                       if creator and "strap_markup_value" in creator.keys() else 0) or 0

        # v47: tracks whether any saved line's Discount % got silently
        # capped by cost_engine.capped_discount_pct(), so the save response
        # can tell the rep -- see the two "if line_capped:" spots below.
        any_discount_capped = False

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
                # v47: same combined + capped discount rule as the live
                # calculator -- see cost_engine.capped_discount_pct(). Saving
                # re-caps independently of whatever the UI already showed,
                # so the stored price can never reflect more discount than
                # the owner allows. v94 -- Strap's own Max Discount cap.
                discount_pct, line_capped = cost_engine.capped_discount_pct(
                    db, line_discount_pct, global_discount_pct, product_family="strap"
                )
                if line_capped:
                    any_discount_capped = True
                credit_term = _is_credit_term(data)
                # v62 -- only the shipping (freight) leg is shared with
                # Stretch Film's Catalog & Rates > Rates tables now, keyed
                # by this quotation's own Destination; FOB stays Strap's
                # own separate flat per-container setting (Admin > PET/PP
                # Strap Costing) -- see the matching note in
                # _calculate_strap_line() above.
                strap_fob_container_usd = None
                strap_shipping_container_usd = _freight_for_destination(db, destination)
                calc = strap_pricing.compute_strap_line(db, line_product_line, strap_product,
                                                          discount_pct=discount_pct, credit_term=credit_term,
                                                          hidden_markup_mode=creator_strap_markup_mode,
                                                          hidden_markup_value=creator_strap_markup_value,
                                                          fob_container_usd=strap_fob_container_usd,
                                                          shipping_container_usd=strap_shipping_container_usd)
                calc_full = strap_pricing.compute_strap_line(db, line_product_line, strap_product,
                                                               discount_pct=0, credit_term=credit_term,
                                                               hidden_markup_mode=creator_strap_markup_mode,
                                                               hidden_markup_value=creator_strap_markup_value,
                                                               fob_container_usd=strap_fob_container_usd,
                                                               shipping_container_usd=strap_shipping_container_usd)
                # v96 -- owner-confirmed hard ceiling on gross roll weight
                # (20.2kg PET / 12.2kg PP -- strap_pricing.TARGET_GROSS_WEIGHT_KG).
                # suggest_meters_per_coil() only ever proposes a default; a rep
                # can still type in a larger Meters/Coil by hand with nothing
                # stopping them today, so this blocks the WHOLE save (nothing
                # committed yet -- see db.rollback() below) the moment any one
                # line would exceed it, rather than silently saving an
                # overweight roll. The Pricing screen also warns live from the
                # same calc via /api/calculate-line, before the rep even hits Save.
                if calc["gross_weight_exceeded"]:
                    db.rollback()
                    line_label = "PP Strap" if line_product_line == "pp" else "PET Strap"
                    return jsonify({
                        "error": (
                            f"{line_label}: gross roll weight {calc['gross_weight_kg']:.2f}kg "
                            f"exceeds the maximum allowed ({calc['gross_weight_max_kg']:.1f}kg) -- "
                            f"reduce Meters/Coil for this line and try again."
                        )
                    }), 400
                total_kg = cost_engine.round_half_up(calc["gross_weight_kg"] * qty_coils, 2)
                unit_price = calc["cfr_price_kg"]
                unit_price_full = calc_full["cfr_price_kg"]
                # v67 -- freeze this line's own FOB $/kg alongside the CFR
                # $/kg above, so the saved quotation's view/PDF/Excel can
                # show a real per-line FOB column for strap lines -- see
                # the fob_price_usd_kg column's comment in db._migrate().
                fob_price_usd_kg = calc["fob_price_kg"]
                if is_custom:
                    db.execute(
                        """INSERT INTO quotation_line
                           (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                            unit_price_usd_kg, unit_price_full_usd_kg, fob_price_usd_kg, total_kg,
                            line_discount_pct, pricing_basis, product_line, strap_custom_bom_key,
                            strap_custom_width_mm, strap_custom_thickness_mm, strap_custom_meters_per_coil,
                            strap_custom_core_weight_kg, strap_custom_has_box, strap_custom_ctr20,
                            strap_custom_ctr40, strap_custom_has_pallet)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (quotation_id, None, "Credit" if credit_term else "Cash", "Per Coil",
                         qty_pallets_display, unit_price, unit_price_full, fob_price_usd_kg, total_kg,
                         line_discount_pct, "per_coil", line_product_line, strap_product["bom_key"],
                         strap_product["width_mm"], strap_product["thickness_mm"],
                         strap_product["meters_per_coil"], strap_product["core_weight_kg"],
                         int(strap_product["has_box"]), int(strap_product["ctr20"]),
                         int(strap_product["ctr40"]), int(strap_product["has_pallet"])),
                    )
                else:
                    db.execute(
                        """INSERT INTO quotation_line
                           (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                            unit_price_usd_kg, unit_price_full_usd_kg, fob_price_usd_kg, total_kg,
                            line_discount_pct, pricing_basis, product_line)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (quotation_id, strap_product_id, "Credit" if credit_term else "Cash", "Per Coil",
                         qty_pallets_display, unit_price, unit_price_full, fob_price_usd_kg, total_kg,
                         line_discount_pct, "per_coil", line_product_line),
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
            # v47: then capped at the admin-configured max -- see
            # cost_engine.capped_discount_pct().
            line_discount_pct = float(l.get("line_discount_pct") or 0)
            discount_pct, line_capped = cost_engine.capped_discount_pct(
                db, line_discount_pct, global_discount_pct
            )
            if line_capped:
                any_discount_capped = True
            # v79 -- same flat credit-term $/kg surcharge Strap lines get
            # above (Extras > Credit payment terms extra) -- see
            # _is_credit_term()/cost_engine.credit_term_extra_usd_kg().
            credit_term = _is_credit_term(data)

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
                    hidden_markup_mode=creator_stretch_markup_mode, hidden_markup_value=creator_stretch_markup_value,
                    credit_term=credit_term,
                )
                unit_price_full, _ = compute_prestretch_line(
                    db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0),
                    roll_weight_kg, core_weight_kg, rolls_per_pallet, packaging_type,
                    price_adjustment_usd_kg=adjustment, pricing_basis=pricing_basis,
                    seller_type=creator_seller_type, colored=colored, discount_pct=0,
                    hidden_markup_mode=creator_stretch_markup_mode, hidden_markup_value=creator_stretch_markup_value,
                    credit_term=credit_term,
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
                hidden_markup_mode=creator_stretch_markup_mode, hidden_markup_value=creator_stretch_markup_value,
                credit_term=credit_term,
            )
            unit_price_full, _ = compute_line(
                db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0),
                price_adjustment_usd_kg=adjustment, pallet_type=pallet_type, pricing_basis=pricing_basis,
                roll_weight_kg=custom_roll_weight_kg, core_weight_kg=custom_core_weight_kg,
                width_mm=custom_width_mm, rolls_per_pallet_override=custom_rolls_per_pallet,
                seller_type=creator_seller_type, auto_manual_override=auto_manual_override, colored=colored,
                discount_pct=0, uv_type=uv_type,
                hidden_markup_mode=creator_stretch_markup_mode, hidden_markup_value=creator_stretch_markup_value,
                credit_term=credit_term,
            )
            # v70.2 -- raw unrounded price, stored so the saved quotation's
            # view/PDF/Excel FOB $/KG can match Stretch!AO exactly (see
            # load_quotation()'s fob_unit computation).
            unit_price_raw, _ = compute_line(
                db, product, country_class, customer_class, float(l.get("quantity_pallets") or 0),
                price_adjustment_usd_kg=adjustment, pallet_type=pallet_type, pricing_basis=pricing_basis,
                roll_weight_kg=custom_roll_weight_kg, core_weight_kg=custom_core_weight_kg,
                width_mm=custom_width_mm, rolls_per_pallet_override=custom_rolls_per_pallet,
                seller_type=creator_seller_type, auto_manual_override=auto_manual_override, colored=colored,
                discount_pct=discount_pct, uv_type=uv_type,
                hidden_markup_mode=creator_stretch_markup_mode, hidden_markup_value=creator_stretch_markup_value,
                credit_term=credit_term,
                round_result=False,
            )
            # v90 -- which container size (40ft/20ft) this line is quoted
            # for, purely so the printed Pallets/Container figure can show
            # the one number that matches -- see quotation_line.container_pref.
            container_pref = l.get("container_pref") or "40ft"
            if container_pref not in ("40ft", "20ft"):
                container_pref = "40ft"
            db.execute(
                """INSERT INTO quotation_line
                   (quotation_id, product_id, pallet_type, packing_type, quantity_pallets,
                    unit_price_usd_kg, unit_price_usd_kg_raw, unit_price_full_usd_kg, total_kg,
                    line_discount_pct, pricing_basis, colored,
                    custom_roll_weight_kg, custom_core_weight_kg, custom_width_mm, custom_rolls_per_pallet, uv_type,
                    container_pref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (quotation_id, product["id"], pallet_type,
                 l.get("packing_type", "Automatic"), float(l.get("quantity_pallets") or 0),
                 unit_price, unit_price_raw, unit_price_full, total_kg, line_discount_pct, pricing_basis,
                 int(colored),
                 (float(custom_roll_weight_kg) if custom_roll_weight_kg not in (None, "") else None),
                 (float(custom_core_weight_kg) if custom_core_weight_kg not in (None, "") else None),
                 (float(custom_width_mm) if custom_width_mm not in (None, "") else None),
                 (float(custom_rolls_per_pallet) if custom_rolls_per_pallet not in (None, "") else None),
                 uv_type, container_pref),
            )

        db.commit()
        q = db.execute("SELECT * FROM quotation WHERE id=?", (quotation_id,)).fetchone()
        total = compute_totals(db, q)["total"]
        return jsonify({
            "id": quotation_id, "quotation_no": q["quotation_no"], "total": total,
            "discount_capped": any_discount_capped,
            # v99 -- tells the admin, on a fresh "Act as [salesperson]" save,
            # which sales rep this new quotation actually got attributed to
            # (created_by_id), so it's never a silent surprise.
            "acting_as_username": (acting_as_user["full_name"] or acting_as_user["username"]) if acting_as_user else None,
        })

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
            """SELECT ql.*, p.stretch_ability, p.micron, p.is_prestretch,
                      p.roll_weight_kg AS p_roll_weight_kg, p.core_weight_kg AS p_core_weight_kg,
                      p.width_mm AS p_width_mm, p.rolls_per_pallet AS p_rolls_per_pallet,
                      sp.code AS strap_code, sp.bom_key AS sp_bom_key, sp.width_mm AS sp_width_mm,
                      sp.thickness_mm AS sp_thickness_mm, sp.core_weight_kg AS sp_core_weight_kg,
                      sp.meters_per_coil AS sp_meters_per_coil
               FROM quotation_line ql
               LEFT JOIN product p ON p.id = ql.product_id
                    AND (ql.product_line IS NULL OR ql.product_line = 'stretch_film')
               LEFT JOIN strap_product sp ON sp.id = ql.product_id
                    AND ql.product_line IN ('pet', 'pp')
               WHERE ql.quotation_id=?""",
            (qid,),
        ).fetchall()
        basis_labels = {"gross": "$/Roll (Gross)", "net": "$/Roll (Net)", "per_kg": "$/KG",
                         "per_coil": "$/Roll"}
        # v67 -- per-line FOB $/KG and CIF $/KG, shown on the saved-
        # quotation view page, PDF and Excel (previously only the plain
        # "Unit $/KG" -- EX-Work for Stretch, CFR for Strap -- and the
        # quotation-level FOB/CIF Total were shown there, unlike the live
        # quote builder, which already shows a per-line FOB/CIF $/KG on
        # screen -- see pricing.html's rowCalc loop / strap fob/cfr cells.
        # For a Stretch Film line, computed live the exact same way the
        # quote builder and compute_totals() do -- EX-Work (frozen
        # unit_price_usd_kg) + the CURRENT loading port's addon spread
        # over this line's own total_kg, never a proportional share of the
        # whole quote -- looked up fresh from the Rates tables just like
        # compute_totals()'s own FOB/CIF Total always has been (never
        # frozen at save time). For a Strap line, the frozen
        # fob_price_usd_kg saved alongside it at save time (None on a line
        # saved before v67 -- shown as "-" rather than guessed).
        fob_addon = _fob_addon_for_port(db, q["loading_port"] if "loading_port" in q.keys() else None)
        freight_amt = _freight_for_destination(db, q["destination"] if "destination" in q.keys() else None)
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
                # v41 -- µ (U+00B5 MICRO SIGN), not μ (U+03BC Greek mu) --
                # see pricing.product_label()'s comment: the Greek letter
                # has no glyph in reportlab's PDF font and was rendering as
                # a bare "m" in every exported quotation ("17μm" -> "17mm").
                label = f"{l['micron']}µm – {l['stretch_ability']}" if l["stretch_ability"] else "-"
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
            # v42 -- a strap line's own pallet_type/packing_type DB columns
            # hold an unrelated internal marker (see the INSERT in
            # api_save_quotation()), not a real Pallet/Packing value, so the
            # exported PDF/Excel show these instead: whether the line ships
            # on a pallet at all, and whether it's boxed.
            strap_pallet_display = strap_packing_display = None
            if line_pl in ("pet", "pp"):
                has_pallet_val = (bool(l["strap_custom_has_pallet"])
                                   if "strap_custom_has_pallet" in l.keys() and l["strap_custom_has_pallet"] is not None
                                   else True)
                has_box_val = (bool(l["strap_custom_has_box"])
                                if "strap_custom_has_box" in l.keys() and l["strap_custom_has_box"] is not None
                                else False)
                strap_pallet_display = "Pallet" if has_pallet_val else "No Pallet"
                strap_packing_display = "Box" if has_box_val else "No Box"

            # v68 -- physical roll-spec details (Width, Roll weight, Core
            # weight -- Thickness and Meters/coil too for Strap) shown as a
            # note row right under each line on the exported PDF/Excel and
            # the saved-quotation view page, mirroring what the live quote
            # builder (pricing.html) already shows next to every line while
            # it's being built (f-width/f-rollwt/f-corewt for Stretch;
            # f-strap-width/-thickness/-meters/-rollwt for Strap). A custom
            # per-line override (custom_width_mm etc / strap_custom_*)
            # wins when set; otherwise falls back to the catalog product's
            # own spec. Left None when nothing is known either way (e.g. a
            # Pre-Stretch line, which has no catalog Width at all).
            spec = None
            if line_pl in ("pet", "pp"):
                if "strap_custom_core_weight_kg" in l.keys() and l["strap_custom_core_weight_kg"]:
                    spec_width = l["strap_custom_width_mm"]
                    spec_thickness = l["strap_custom_thickness_mm"]
                    spec_meters = l["strap_custom_meters_per_coil"]
                    spec_core = l["strap_custom_core_weight_kg"]
                    spec_bom_key = l["strap_custom_bom_key"]
                else:
                    spec_width = l["sp_width_mm"]
                    spec_thickness = l["sp_thickness_mm"]
                    spec_meters = l["sp_meters_per_coil"]
                    spec_core = l["sp_core_weight_kg"]
                    spec_bom_key = l["sp_bom_key"]
                spec_roll = _strap_gross_weight_kg(db, line_pl, spec_bom_key, spec_width,
                                                    spec_thickness, spec_meters, spec_core)
                if spec_width or spec_core or spec_roll:
                    spec = {"width_mm": spec_width, "thickness_mm": spec_thickness,
                            "meters_per_coil": spec_meters, "core_weight_kg": spec_core,
                            "roll_weight_kg": spec_roll}
            elif not (l["is_prestretch"] if "is_prestretch" in l.keys() else False):
                spec_width = (l["custom_width_mm"] if ("custom_width_mm" in l.keys() and l["custom_width_mm"])
                              else l["p_width_mm"])
                spec_roll = (l["custom_roll_weight_kg"] if ("custom_roll_weight_kg" in l.keys() and l["custom_roll_weight_kg"])
                             else l["p_roll_weight_kg"])
                spec_core = (l["custom_core_weight_kg"] if ("custom_core_weight_kg" in l.keys() and l["custom_core_weight_kg"])
                             else l["p_core_weight_kg"])
                if spec_width or spec_roll or spec_core:
                    spec = {"width_mm": spec_width, "roll_weight_kg": spec_roll, "core_weight_kg": spec_core}
            else:
                spec_roll = l["prestretch_roll_weight_kg"] if "prestretch_roll_weight_kg" in l.keys() else None
                spec_core = l["prestretch_core_weight_kg"] if "prestretch_core_weight_kg" in l.keys() else None
                if spec_roll or spec_core:
                    spec = {"width_mm": None, "roll_weight_kg": spec_roll, "core_weight_kg": spec_core}
            spec_note = _format_spec_note(spec) if spec else None

            # v76.1 -- Rolls/Pallet and Pallets/Container as their own
            # columns on the exported PDF/Excel/view page (previously only
            # shown live in the quote builder, or buried in a Strap line's
            # "Stuffing —" note), on the owner's explicit request. Strap
            # reuses the "stuffing" figures above (a single resolved
            # container choice, 20ft or 40ft, per the line's own Box/
            # Container inputs).
            # v90 -- Stretch Film used to show BOTH the 40ft and 20ft
            # capacities together ("34/20"), which read as one confusing
            # combined number -- owner asked for just the one she's actually
            # quoting. It now picks a single figure using the line's own
            # container_pref (40ft/20ft), the same way Strap already picks
            # one via its Box/Container inputs. Both container labels are
            # spelled out ("40ft"/"20ft") rather than with an apostrophe
            # (was "40'"), which read as an inch mark. Pre-Stretch lines
            # aren't stuffed into a container via the packing-tier table at
            # all, so Pallets/Container is left blank for them; Rolls/Pallet
            # is still the rep's own saved figure.
            rolls_per_pallet_display = pallets_per_container_display = None
            if line_pl in ("pet", "pp"):
                if stuffing:
                    rolls_per_pallet_display = stuffing["rolls_per_pallet"]
                    pallets_per_container_display = f"{stuffing['pallets_per_container']} ({stuffing['container']})"
            elif not (l["is_prestretch"] if "is_prestretch" in l.keys() else False):
                eff_roll_weight = spec["roll_weight_kg"] if spec else l["p_roll_weight_kg"]
                eff_auto_manual = l["packing_type"] if ("packing_type" in l.keys() and l["packing_type"]) else "Automatic"
                custom_rpp = l["custom_rolls_per_pallet"] if "custom_rolls_per_pallet" in l.keys() else None
                rolls_per_pallet_display = cost_engine.effective_rolls_per_pallet(
                    db, {"auto_manual": eff_auto_manual, "roll_weight_kg": eff_roll_weight,
                         "rolls_per_pallet": l["p_rolls_per_pallet"]},
                    l["pallet_type"], custom_rpp)
                tier = cost_engine.lookup_packing_tier(db, eff_auto_manual, eff_roll_weight, l["pallet_type"])
                if tier and (tier["pallets_per_container40"] or tier["pallets_per_container20"]):
                    c40 = tier["pallets_per_container40"]
                    c20 = tier["pallets_per_container20"]
                    line_container_pref = l["container_pref"] if "container_pref" in l.keys() and l["container_pref"] else "40ft"
                    chosen = c40 if line_container_pref == "40ft" else c20
                    chosen = chosen or c40 or c20  # fall back if the preferred size has no figure at all
                    if chosen:
                        pallets_per_container_display = f"{chosen:g} ({line_container_pref})"
            else:
                rolls_per_pallet_display = l["prestretch_rolls_per_pallet"] if "prestretch_rolls_per_pallet" in l.keys() else None

            if line_pl in ("pet", "pp"):
                # v81 -- owner-confirmed: Strap is ALWAYS quoted $/Roll, never
                # $/KG -- unlike Stretch Film, their customers' own buying
                # convention doesn't work in $/KG at all. fob_price_usd_kg /
                # unit_price_usd_kg are still what's stored (same frozen
                # Cash/Credit FOB and CFR $/kg strap_pricing.compute_strap_line()
                # produces -- the underlying cost math is untouched), so the
                # $/Roll figure shown here is just that stored $/kg times
                # this line's own gross roll weight (spec["roll_weight_kg"],
                # net+core -- computed just above), the same multiplication
                # compute_strap_line() itself does internally to go from
                # fob_price_kg to fob_price_roll. See build_pdf()/build_xlsx()/
                # view_quotation.html for the matching "always $/Roll for
                # Strap, in its own table when mixed with Stretch" split.
                roll_wt = spec["roll_weight_kg"] if spec else None
                fob_kg = (l["fob_price_usd_kg"] if ("fob_price_usd_kg" in l.keys()
                                                      and l["fob_price_usd_kg"] is not None) else None)
                cif_kg = l["unit_price_usd_kg"]  # frozen, freight-inclusive CFR $/kg
                fob_unit = (cost_engine.round_half_up(fob_kg * roll_wt, 2)
                            if (fob_kg is not None and roll_wt) else None)
                cif_unit = (cost_engine.round_half_up(cif_kg * roll_wt, 2)
                            if roll_wt else cif_kg)
            else:
                # v70.2 -- the sheet's Stretch!AO (FOB $/KG) applies Excel's
                # ROUNDUP() (ceiling) to the UNROUNDED EX-Work price plus the
                # port addon, not to the already-2dp-rounded price. Use the
                # raw price when we have it (rows saved after this fix);
                # fall back to the rounded price for old rows.
                raw_base = (l["unit_price_usd_kg_raw"] if ("unit_price_usd_kg_raw" in l.keys()
                            and l["unit_price_usd_kg_raw"] is not None) else l["unit_price_usd_kg"])
                fob_unit = cost_engine.round_up(
                    raw_base + (fob_addon / l["total_kg"] if l["total_kg"] else 0), 2)
                cif_unit = cost_engine.round_half_up(
                    fob_unit + (freight_amt / l["total_kg"] if l["total_kg"] else 0), 2)

            lines.append(dict(l, label=label, line_total=line_total, line_total_full=line_total_full,
                               pricing_basis_label=basis_labels.get(basis, "$/KG"), stuffing=stuffing,
                               spec=spec, spec_note=spec_note,
                               strap_pallet_display=strap_pallet_display, strap_packing_display=strap_packing_display,
                               fob_unit_usd_kg=fob_unit, cif_unit_usd_kg=cif_unit,
                               rolls_per_pallet_display=rolls_per_pallet_display,
                               pallets_per_container_display=pallets_per_container_display))
        totals = compute_totals(db, q, lines)
        return q, lines, totals

    def _format_spec_note(spec):
        """v68 -- renders a line's roll-spec dict (see load_quotation) into
        the single 'Width: ... · ...' note string shown under the line on
        the view page / PDF / Excel. Any field that's genuinely unknown for
        that line is left out rather than shown as 0.
        v76 -- Roll weight / Core weight dropped from this note: they're now
        their own dedicated columns in the line table (see build_pdf() /
        build_xlsx() / view_quotation.html), replacing the Unit Price/Line
        Total columns the owner said she never uses, so repeating them here
        too would just be clutter."""
        parts = []
        if spec.get("width_mm"):
            parts.append(f"Width: {spec['width_mm']:g}mm")
        if spec.get("thickness_mm"):
            parts.append(f"Thickness: {spec['thickness_mm']:g}mm")
        if spec.get("meters_per_coil"):
            parts.append(f"Meters/coil: {spec['meters_per_coil']:g}")
        return "Spec — " + " · ".join(parts) if parts else None

    def _strap_gross_weight_kg(db, line_key, bom_key, width_mm, thickness_mm, meters_per_coil, core_weight_kg):
        """v68 -- gross weight per coil for a strap line's report spec row,
        computed the same way strap_pricing.compute_strap_line() derives it
        internally (net weight from width/thickness/BOM density + core
        weight) -- reused here purely for display, since quotation_line
        only ever saved the line's TOTAL kg (net of quantity), not a
        reusable per-roll figure."""
        if not (width_mm and thickness_mm and meters_per_coil and bom_key):
            return None
        bom = strap_pricing._get_bom(db, line_key, bom_key)
        product = {"width_mm": width_mm, "thickness_mm": thickness_mm}
        g_per_m = strap_pricing.meter_weight_g_per_m(line_key, product, bom["components"])
        if not g_per_m:
            return None
        roll_net_kg = meters_per_coil * g_per_m / 1000.0
        return cost_engine.round_half_up(roll_net_kg + (core_weight_kg or 0), 2)

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
                line_pl = (l["product_line"] if "product_line" in l.keys() and l["product_line"]
                           else "stretch_film")
                lines.append({
                    "product_line": line_pl,
                    "line_total": cost_engine.round_half_up(l["unit_price_usd_kg"] * l["total_kg"], 2),
                    "line_total_full": cost_engine.round_half_up(full_unit * l["total_kg"], 2),
                })
        total = cost_engine.round_half_up(sum(l["line_total"] for l in lines), 2)
        subtotal = cost_engine.round_half_up(sum(l.get("line_total_full", l["line_total"]) for l in lines), 2)

        # v61 -- Strap (PET/PP) line totals are already fully freight- and
        # port-inclusive (each strap line spreads its own container's FOB
        # handling + international freight into its stored CFR $/kg via
        # strap_pricing.compute_strap_line()'s _container_share()). Only
        # Stretch Film lines are stored EX-Work, so the quotation-level FOB/
        # CIF add-on below must apply ONLY to the Stretch Film portion of
        # `total` -- adding it on top of the (already freight-inclusive)
        # strap total would double-count one port-handling fee and one
        # freight amount for any quotation containing strap lines.
        has_stretch_line = any((l.get("product_line") or "stretch_film") == "stretch_film" for l in lines)
        stretch_total = cost_engine.round_half_up(
            sum(l["line_total"] for l in lines
                if (l.get("product_line") or "stretch_film") == "stretch_film"), 2)
        non_stretch_total = cost_engine.round_half_up(total - stretch_total, 2)

        # FOB Total = EX-Work total (after discount) + the selected loading
        # port's flat handling/customs/trucking add-on (Alexandria vs
        # Damietta -- editable in Admin > Loading Ports, since these rates
        # move), applied to the Stretch Film portion only (see note above);
        # any strap lines are added back in as-is, already CFR-priced. If
        # the quotation has NO Stretch Film lines at all (strap-only), the
        # add-on isn't applied a second time on top of strap's own
        # already-inclusive pricing -- fob_total/cif_total just equal the
        # (already CFR) strap total. CIF Total = FOB Total + freight to the
        # selected destination (from the existing Freight table), same
        # Stretch-only rule. Both are shown to the client as the FOB and
        # CIF offers side by side; the raw freight $ figure itself is not
        # broken out as its own line, same treatment as the hidden
        # foreign-seller markup.
        fob_addon = _fob_addon_for_port(db, q["loading_port"] if "loading_port" in q.keys() else None) if has_stretch_line else 0
        fob_total = cost_engine.round_half_up(stretch_total + fob_addon + non_stretch_total, 2)
        freight_amt = _freight_for_destination(db, q["destination"] if "destination" in q.keys() else None) if has_stretch_line else 0
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

        def clean_mode(field):
            m = request.form.get(field, "percent")
            return m if m in ("percent", "cents_per_kg") else "percent"

        # v46 -- independent hidden markup per product family -- see
        # user.stretch_markup_mode/value + strap_markup_mode/value.
        stretch_markup_mode = clean_mode("stretch_markup_mode")
        strap_markup_mode = clean_mode("strap_markup_mode")
        db.execute(
            """UPDATE user SET full_name=?, role=?, region=?, active=?, price_adjustment_usd_kg=?,
               seller_type=?, stretch_markup_mode=?, stretch_markup_value=?,
               strap_markup_mode=?, strap_markup_value=? WHERE id=?""",
            (
                request.form.get("full_name", "").strip(),
                request.form.get("role", "sales_rep"),
                request.form.get("region", "").strip(),
                1 if request.form.get("active") == "on" else 0,
                float(request.form.get("price_adjustment_usd_kg") or 0),
                request.form.get("seller_type", "local"),
                stretch_markup_mode,
                float(request.form.get("stretch_markup_value") or 0),
                strap_markup_mode,
                float(request.form.get("strap_markup_value") or 0),
                uid,
            ),
        )
        # v92 -- "New password" is its own optional field on the same Save
        # form/row (admin_users.html): left blank, nothing changes; typed
        # in, it resets that user's login password right here -- there was
        # previously no way at all to change a password once the account
        # existed (Password only ever appeared on the one-time Add user
        # form), which is what the owner asked about.
        new_password = request.form.get("new_password", "").strip()
        if new_password:
            db.execute(
                "UPDATE user SET password_hash=? WHERE id=?",
                (generate_password_hash(new_password), uid),
            )
        db.commit()
        flash("User updated." + (" Password changed." if new_password else ""), "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:uid>/delete", methods=["POST"])
    @admin_required
    def admin_user_delete(uid):
        """v89 -- was missing entirely (only create/update existed); the
        "Active" checkbox on the Users page was the only way to retire an
        account. Deleting outright is refused, with a clear reason, in the
        two cases where it would either break the app or silently erase
        the audit trail on saved quotations: deleting yourself while
        signed in, and deleting a user who still has quotations attributed
        to them (quotation.created_by_id -> user.id is a real foreign key,
        enforced -- see db.get_db()'s PRAGMA foreign_keys = ON -- so this
        would otherwise fail with a raw DB error instead of an explanation).
        In the second case, deactivating the account (the checkbox) is the
        safe alternative -- it keeps every past quotation's "who made this
        quote" intact for the sales history, while stopping that user from
        logging in again."""
        db = g.db
        if uid == g.user["id"]:
            flash("You can't delete the account you're currently signed in as.", "error")
            return redirect(url_for("admin_users"))
        target = db.execute("SELECT * FROM user WHERE id=?", (uid,)).fetchone()
        if not target:
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))
        quote_count = db.execute(
            "SELECT COUNT(*) c FROM quotation WHERE created_by_id=?", (uid,)
        ).fetchone()["c"]
        if quote_count:
            flash(
                f"Can't delete {target['username']}: {quote_count} saved quotation(s) are still "
                "attributed to them, and deleting would break that history. Uncheck \"Active\" "
                "instead to stop them from signing in without losing the record of what they quoted.",
                "error",
            )
            return redirect(url_for("admin_users"))
        db.execute("DELETE FROM user WHERE id=?", (uid,))
        db.commit()
        flash(f"User {target['username']} deleted.", "success")
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
        # the dedicated Strap Costing page instead. (v89 -- dollar_rate here
        # is Stretch Film's own now; Strap got its own separate
        # strap_dollar_rate, which lives on the Strap Costing page along
        # with everything else strap_-prefixed -- see db.py's migration.)
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
        PET Strap / PP Strap lines: their own dollar rate, material prices,
        BOM recipes (composition/profit/waste), per-line fixed cost/
        electricity/direct labor, FOB cost per container, and the
        credit-term surcharge setting. v62 -- only the shipping/freight-
        rate-per-container setting moved out of here: Strap's shipping leg
        now uses the shared Freight table (Admin > Catalog & Rates >
        Freight) that Stretch Film uses, keyed by the quotation's own
        Destination; FOB cost per container stays Strap's own separate
        setting, per the owner's clarification. v88 -- Dollar Rate is now
        Strap's own separate setting too (owner-requested split from
        Stretch Film's -- they used to share one row; see
        strap_pricing._dollar_rate()'s docstring)."""
        db = g.db
        if request.method == "POST":
            # v88 -- Strap's own independent Dollar Rate (no longer shared
            # with Stretch Film's Global Settings row).
            val = request.form.get("strap_dollar_rate")
            if val is not None and val != "":
                db.execute("UPDATE global_setting SET value=? WHERE key='strap_dollar_rate'", (float(val),))

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
                # v87 -- Fixed cost / Direct labor are now EGP/ton (live-
                # divided by the Dollar Rate at calc time, like Electricity
                # just above) instead of a frozen $/kg figure -- see
                # strap_pricing._get_line_config()'s docstring.
                if fixed_val is not None and fixed_val != "":
                    db.execute("UPDATE strap_line_config SET fixed_cost_per_ton_egp=? WHERE line_key=?",
                               (float(fixed_val), line_key))
                if labor_val is not None and labor_val != "":
                    db.execute("UPDATE strap_line_config SET direct_labor_per_ton_egp=? WHERE line_key=?",
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

        # v88 -- Strap's own Dollar Rate (independent from Stretch Film's).
        dollar_rate = db.execute("SELECT value FROM global_setting WHERE key='strap_dollar_rate'").fetchone()
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
        # v62 -- only strap_shipping_rate_per_container_usd is retired from
        # this page: Strap's shipping/freight leg now looks up the shared
        # Freight table (Admin > Catalog & Rates > Freight), same as
        # Stretch Film -- see _freight_for_destination() and every
        # strap_pricing.compute_strap_line() call site. FOB cost per
        # container stays here as Strap's own separate flat setting (the
        # owner's clarification: only freight is shared, not FOB).
        # v88 -- strap_dollar_rate also excluded here: it gets its own
        # dedicated "Dollar Rate" section on the page (see dollar_rate
        # above), not the generic FOB/credit-terms grid. v94 --
        # strap_max_discount_pct excluded the same way, its own "Max
        # Discount" section below (see max_discount below).
        freight = db.execute(
            "SELECT * FROM global_setting WHERE key LIKE 'strap_%' "
            "AND key NOT IN ('strap_shipping_rate_per_container_usd', 'strap_dollar_rate', "
            "'strap_max_discount_pct') "
            "ORDER BY label"
        ).fetchall()
        # v94 -- Strap's own Max Discount cap, independent of Stretch
        # Film's (Global Cost Settings) -- see
        # cost_engine.capped_discount_pct()/db._seed_strap_data().
        max_discount = db.execute("SELECT value FROM global_setting WHERE key='strap_max_discount_pct'").fetchone()
        return render_template(
            "admin_strap_costing.html",
            dollar_rate=dollar_rate["value"] if dollar_rate else 45,
            material_rates=material_rates,
            boms=boms,
            line_configs=line_configs,
            freight=freight,
            max_discount=max_discount["value"] if max_discount else 2.0,
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
    # v69 -- explicit left/right margins (previously reportlab's 1in/72pt
    # default on each side, tighter than it looked -- the line-items table
    # was already drawing slightly past that default frame). 15mm matches
    # the letterhead/divider's own 180mm content width on an A4 (210mm)
    # page, and frees up real room for the wider columns below.
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=14 * mm, bottomMargin=20 * mm,
                             leftMargin=15 * mm, rightMargin=15 * mm)
    # v75 -- owner-reported letterhead layout pass: the page content area is
    # 180mm wide (15mm margins each side on A4's 210mm) -- 180mm == 510.24pt
    # == PAGE_CONTENT_WIDTH below, and EVERY top-level flowable (letterhead,
    # divider, meta table, line-items table, totals table) is now sized to
    # that exact width AND explicitly left-aligned (hAlign="LEFT"), so they
    # all share one true left AND right edge down the page. Before this,
    # meta_table/totals_table were narrower than the letterhead/line-items
    # table (480pt/490pt vs 510pt) -- and reportlab's Table defaults to
    # CENTER alignment in its frame when no hAlign is set, so those two
    # narrower tables rendered visibly inset/off-center from everything
    # else instead of flush-left with it, the "مافيش alignment" the owner
    # flagged.
    PAGE_CONTENT_WIDTH = 180 * mm

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
        # v75 -- breathing room between the bold company NAME and the
        # Address line right under it (previously 1pt top padding on every
        # row, including this one -- the name and "Address :" sat almost
        # touching). Only the address row (index 1) gets the extra gap; the
        # Tel/Tel2/Tel3/Fax/Email block underneath stays tight, as before.
        ("TOPPADDING", (0, 1), (0, 1), 6),
    ]))

    if os.path.exists(logo_path):
        # v75 -- logo made a bit larger (26mm -> 30mm) and vertically
        # centered against the company-info block (was TOP-aligned, which
        # left dead space under a short logo next to the taller 7-line
        # address block) so the letterhead reads as one balanced unit
        # spanning the full page width, not a small icon floating at the
        # top of a wide empty column.
        logo = RLImage(logo_path, width=30 * mm, height=30 * mm * (246 / 209))
        letterhead = Table([[logo, company_table]], colWidths=[36 * mm, PAGE_CONTENT_WIDTH - 36 * mm])
        letterhead.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("LEFTPADDING", (1, 0), (1, 0), 8),
        ]))
    else:
        letterhead = company_table
    letterhead.hAlign = "LEFT"

    elements = [letterhead, Spacer(1, 10)]
    divider = Table([[""]], colWidths=[PAGE_CONTENT_WIDTH], rowHeights=[0.75],
                     style=TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1, colors.HexColor("#cccccc"))]))
    divider.hAlign = "LEFT"
    elements.append(divider)
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
    # v75 -- widened from [90,150,90,150] (480pt) to sum to the full
    # PAGE_CONTENT_WIDTH (510pt) -- see the v75 note above build_pdf().
    meta_table = Table(meta, colWidths=[95, 160, 95, PAGE_CONTENT_WIDTH - 95 - 160 - 95])
    meta_table.hAlign = "LEFT"
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
    # v41 -- the Product label (especially a strap line's verbose "Custom
    # WxT (BOM label)" text) is long enough to overflow its column width.
    # reportlab does NOT wrap a plain string in a Table cell -- it just
    # draws it past the column edge, visually overlapping whatever text
    # sits in the next column(s) over on the same row (this is what made
    # strap rows unreadable: the product label was drawing on top of the
    # Pallet/Packing columns' own text). Wrapping it in a Paragraph makes
    # reportlab wrap it onto multiple lines within its own column instead.
    label_style = ParagraphStyle("LineLabel", parent=styles["Normal"], fontSize=8, leading=10)
    basis_style = ParagraphStyle("LineBasis", parent=label_style, alignment=2)  # 2 = TA_RIGHT

    # v67 -- FOB $/KG / CIF $/KG columns added (previously only the plain
    # "Unit $/KG" -- see load_quotation()'s comment on fob_unit_usd_kg/
    # cif_unit_usd_kg for how each is computed).
    # v69 -- every header now wrapped in a Paragraph (previously only the
    # $/KG columns were, since reportlab does NOT wrap a plain string --
    # it draws it past the column edge and overlaps the neighbouring
    # header, the same overlap bug the v41 comment above describes for
    # line data). Labels also reworded, on the owner's request, so each
    # column reads unambiguously as a quantity ("Qty (Pallets)", "Total
    # Qty (KG)") or a money amount in USD ("Unit Price ($/KG)" etc) rather
    # than a bare, easy-to-misread number -- "Total KG" in particular used
    # to read like it might be a second total/amount column next to "Line
    # Total $", when it's really just this line's own quantity in KG.
    # v76.1 -- Qty (Pallets) dropped, Rolls/Pallet and Pallets/Container
    # added as their own columns (previously only shown live in the quote
    # builder, or for Strap only, buried in a "Stuffing —" note row), on
    # the owner's explicit, column-by-column instruction. The old
    # "Stuffing —" note is gone too -- Rolls/Pallet, Pallets/Container and
    # Box (via the Packing column's "Box"/"No Box") are now all real
    # columns instead, so the note would just repeat them.
    header_style = ParagraphStyle("LineHeader", parent=styles["Normal"], fontSize=7.5, leading=9,
                                   textColor=colors.white, alignment=1)  # 1 = TA_CENTER

    # v81 -- owner-confirmed: Strap is always quoted $/Roll, Stretch Film
    # always $/KG -- the two can no longer share one FOB/CIF header label,
    # so the line-items table is now built by this helper (one call per
    # product family) instead of inline once over the whole `lines` list.
    # When a quotation mixes both families, build_pdf() below calls this
    # twice and renders two separate tables (own heading each) with the
    # combined FOB/CIF grand totals unchanged underneath; a single-family
    # quotation still gets exactly one table, just as before v81.
    def _line_items_table(family_lines, dollar_unit, start_num):
        # v76.2 -- Total Qty (KG) dropped too, on the owner's follow-up request.
        header = [Paragraph(t, header_style) for t in
                  ["#", "Product", "Pallet", "Packing", "Basis",
                   "Roll Weight<br/>(kg)", "Core Weight<br/>(kg)", "Rolls/<br/>Pallet",
                   "Pallets/<br/>Container", f"FOB Price<br/>({dollar_unit})",
                   f"CIF Price<br/>({dollar_unit})"]]
        rows = [header]
        span_commands = []
        num = start_num
        for line in family_lines:
            # v41 -- pallet_type/packing_type on a PET/PP Strap line don't hold
            # a real pallet/packing choice: those two columns are reused
            # internally to carry the line's payment-term surcharge state
            # ("Cash"/"Credit" and a fixed "Per Coil" marker -- see
            # api_save_quotation()'s strap branch). v42 -- load_quotation()
            # now works out a real Pallet/Packing value for a strap line from
            # its own saved Pallet/Box checkboxes (strap_pallet_display /
            # strap_packing_display) instead of leaving the column blank.
            is_strap_line = line.get("product_line") in ("pet", "pp")
            # v41 -- Pallet/Packing/Basis are also wrapped in a Paragraph, not
            # just Product: "Standard Pallet"/"Automatic" are plain strings
            # too wide for their column at this font size, and a plain string
            # overflows into the next column instead of wrapping, the same
            # collision bug the Product column had.
            fob_unit = line.get("fob_unit_usd_kg")
            cif_unit = line.get("cif_unit_usd_kg")
            spec = line.get("spec") or {}
            roll_wt = spec.get("roll_weight_kg")
            core_wt = spec.get("core_weight_kg")
            rpp = line.get("rolls_per_pallet_display")
            ppc = line.get("pallets_per_container_display")
            rows.append([
                str(num), Paragraph(line["label"], label_style),
                Paragraph(line["strap_pallet_display"] if is_strap_line else line["pallet_type"], label_style),
                Paragraph(line["strap_packing_display"] if is_strap_line else line["packing_type"], label_style),
                Paragraph(line.get("pricing_basis_label", "$/KG"), basis_style),
                f"{roll_wt:g}" if roll_wt else "-",
                f"{core_wt:g}" if core_wt else "-",
                f"{rpp:g}" if rpp else "-",
                Paragraph(ppc, basis_style) if ppc else "-",
                f"{fob_unit:.2f}" if fob_unit is not None else "-",
                f"{cif_unit:.2f}" if cif_unit is not None else "-",
            ])
            num += 1
            # v68 -- roll-spec sub-row (Width, plus Thickness/Meters-per-coil
            # for Strap) right under every line that has one -- see
            # load_quotation()'s spec_note comment. Roll weight/Core weight are
            # their own columns now (v76), so no longer repeated here.
            spec_note = line.get("spec_note")
            if spec_note:
                row_idx = len(rows)
                rows.append(["", Paragraph(spec_note, stuffing_style), "", "", "", "", "", "", "", "", ""])
                span_commands.append(("SPAN", (1, row_idx), (-1, row_idx)))
        return rows, span_commands, num
    # v69 -- widened (was [14, 86, 40, 40, 44, 28, 38, 40, 40, 40, 48], sum
    # 458pt) now that the 15mm margins above free up the room -- sums to
    # 508pt, just inside the 510pt usable width on an A4 page with those
    # margins.
    # v75 -- rebalanced (still sums to PAGE_CONTENT_WIDTH, 510pt): the old
    # Pallet(44)/Packing(44)/Basis(46) columns were narrower than their own
    # cell TEXT at this font size -- "Standard Pallet" (~55pt wide at 8pt
    # Helvetica), "Manual(2.3~3.5kg)" (~67pt) and "$/Roll (Gross)" (~49pt)
    # -- so reportlab's Paragraph wrapped them, and because none of those
    # are multi-word phrases with a good break point, the wrap fell mid-
    # word ("Standar"/"d Pallet", "Automati"/"c") -- the same failure class
    # already fixed on the web admin pages' CSS, but this PDF table builds
    # its own layout and needed its own fix. Pallet/Packing/Basis widened
    # to comfortably clear their own longest real value; Product (still
    # meant to wrap for long labels, unchanged in kind) and the narrow
    # numeric columns gave up the room for it, plus tighter cell padding
    # (6pt->4pt each side) below.
    # v76.1 -- Qty(Pallets) column removed, Rolls/Pallet + Pallets/Container
    # added (12 columns now, was 11). Pallet/Packing/Basis/Product kept wide
    # enough to clear their own longest single-line value (measured with
    # stringWidth(), same approach as v75) so nothing wraps mid-word or
    # mid-phrase -- Product in particular needs to comfortably clear a long
    # Strap custom label's last "word" (e.g. "(Green/Natural))", ~58pt --
    # narrowing Product to 65pt in an earlier pass of this fix broke that
    # one mid-word ("N"/"atural))"), caught by the 25-line stress test
    # below. The new Rolls/Pallet and Pallets/Container headers use their
    # own <br/> line breaks (like Rolls/<br/>Pallet) since their column is
    # narrower than the plain header text.
    # v76.2 -- Total Qty(KG) dropped (11 columns now), its 40pt redistributed:
    # mostly back to Product (long Strap custom labels need the room -- see
    # the v76.1 comment above about "(Green/Natural))"), the rest spread
    # across the new physical/packing columns for a touch more breathing
    # room.
    def _make_table(rows, span_commands):
        t = Table(rows, colWidths=[14, 93, 63, 76, 58, 32, 30, 32, 46, 33, 33])
        t.hAlign = "LEFT"
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            # v75 -- LEFTPADDING/RIGHTPADDING reduced for EVERY row including
            # the header (reportlab's own default is 6pt each side): applying
            # this only to the body rows left the header row's own "#" column
            # at its old default 12pt of padding against a narrower 12pt
            # column, i.e. zero room for the header's own "#" -- reportlab
            # doesn't wrap-fail gracefully in that case, it blows up the row
            # height instead (a LayoutError on any quotation long enough to
            # reach a second page). Same 4pt padding everywhere fixes both.
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
            *span_commands,
        ]))
        return t

    # v81 -- Strap is always $/Roll, Stretch Film always $/KG, so the two
    # can't share one table header. A mixed quotation gets two separate
    # tables (own section heading, own "#" numbering continuing across
    # both) instead of one; a single-family quotation still gets exactly
    # one table, unheaded, as before -- only the strap-only case's own
    # FOB/CIF header/values changed (from $/KG to $/Roll), which is the
    # whole point of this change.
    section_style = ParagraphStyle("SectionHeading", parent=styles["Normal"], fontSize=9,
                                    fontName="Helvetica-Bold", textColor=colors.HexColor("#1f2937"),
                                    spaceBefore=0, spaceAfter=4)
    stretch_lines = [l for l in lines if l.get("product_line") not in ("pet", "pp")]
    strap_lines = [l for l in lines if l.get("product_line") in ("pet", "pp")]
    if stretch_lines and strap_lines:
        rows1, spans1, next_num = _line_items_table(stretch_lines, "$/KG", 1)
        elements.append(Paragraph("Stretch Film", section_style))
        elements.append(_make_table(rows1, spans1))
        elements.append(Spacer(1, 10))
        rows2, spans2, _ = _line_items_table(strap_lines, "$/Roll", next_num)
        elements.append(Paragraph("PET / PP Strap", section_style))
        elements.append(_make_table(rows2, spans2))
        elements.append(Spacer(1, 14))
    elif strap_lines:
        rows, spans, _ = _line_items_table(strap_lines, "$/Roll", 1)
        elements.append(_make_table(rows, spans))
        elements.append(Spacer(1, 14))
    else:
        rows, spans, _ = _line_items_table(stretch_lines, "$/KG", 1)
        elements.append(_make_table(rows, spans))
        elements.append(Spacer(1, 14))

    # v96 -- owner-confirmed: no totals with prices (or total weight)
    # anywhere on the Pricing screen or in the PDF/Excel exports, for either
    # Stretch Film or Strap -- this used to end with a Global Discount/FOB
    # Total/CIF Total box; per-line "Line Total"/"Total KG" columns were
    # already dropped from this same table earlier (v76/v76.2). The rep
    # reads FOB/CIF per unit ($/KG or $/Roll) straight off the line-items
    # table above; nothing here sums them into a quote-level dollar figure.
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

    # v67 -- FOB $/KG / CIF $/KG columns added (see the matching comment in
    # build_pdf() and load_quotation()'s fob_unit_usd_kg/cif_unit_usd_kg).
    # v69 -- labels reworded (matching build_pdf()'s own v69 comment) so
    # each column reads unambiguously as a quantity ("Qty (Pallets)",
    # "Total Qty (KG)") or a money amount in USD, and wrapped onto 2 lines
    # (wrap_text + a taller header row) rather than one long string, with
    # wider columns to match.
    # v76 -- Unit Price ($/KG) / Line Total (USD) swapped for Roll Weight
    # (kg) / Core Weight (kg) -- see the matching comment in build_pdf().
    # v76.1 -- Qty (Pallets) dropped, Rolls/Pallet + Pallets/Container added
    # (12 columns now) -- see the matching comment in build_pdf().
    # v76.2 -- Total Qty (KG) dropped too (11 columns now).
    # v81 -- Strap is always $/Roll, Stretch Film always $/KG (see the
    # matching comment in build_pdf()), so the header and line-writing are
    # now their own helpers, called once per product family -- twice, with
    # a section-heading row between them, when a quotation mixes both.
    header_wrap = Alignment(horizontal="center", vertical="center", wrap_text=True)
    stuffing_font = Font(italic=True, size=8, color="666666")
    section_font = Font(bold=True, size=10, color="1F2937")

    def _write_header_row(hdr_row, dollar_unit):
        headers = ["#", "Product", "Pallet", "Packing", "Basis",
                   "Roll Weight\n(kg)", "Core Weight\n(kg)", "Rolls/Pallet",
                   "Pallets/Container", f"FOB Price\n({dollar_unit})",
                   f"CIF Price\n({dollar_unit})"]
        for col, h in enumerate(headers, start=1):
            cell = ws.cell(row=hdr_row, column=col, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.border = border
            cell.alignment = header_wrap
        ws.row_dimensions[hdr_row].height = 28

    def _write_line_rows(start_row, family_lines, start_num):
        r = start_row
        num = start_num
        for line in family_lines:
            # v42 -- see the matching comment in build_pdf(): a PET/PP Strap
            # line's pallet_type/packing_type columns hold an internal
            # payment-term marker ("Cash"/"Credit", "Per Coil"), not a real
            # Pallet/Packing choice -- these use the line's own saved Pallet/
            # Box checkboxes instead (strap_pallet_display/strap_packing_display).
            is_strap_line = line.get("product_line") in ("pet", "pp")
            fob_unit = line.get("fob_unit_usd_kg")
            cif_unit = line.get("cif_unit_usd_kg")
            spec = line.get("spec") or {}
            roll_wt = spec.get("roll_weight_kg")
            core_wt = spec.get("core_weight_kg")
            rpp = line.get("rolls_per_pallet_display")
            ppc = line.get("pallets_per_container_display")
            values = [
                num, line["label"], line["strap_pallet_display"] if is_strap_line else line["pallet_type"],
                line["strap_packing_display"] if is_strap_line else line["packing_type"],
                line.get("pricing_basis_label", "$/KG"),
                roll_wt if roll_wt else "-",
                core_wt if core_wt else "-",
                rpp if rpp else "-",
                ppc if ppc else "-",
                cost_engine.round_half_up(fob_unit, 2) if fob_unit is not None else "-",
                cost_engine.round_half_up(cif_unit, 2) if cif_unit is not None else "-",
            ]
            for col, v in enumerate(values, start=1):
                cell = ws.cell(row=r, column=col, value=v)
                cell.border = border
                if col >= 6:
                    cell.alignment = right
            r += 1
            num += 1
            # v68 -- roll-spec note (Width, plus Thickness/Meters-per-coil for
            # Strap) right under every line that has one -- see
            # load_quotation()'s spec_note comment. Roll weight/Core weight are
            # their own columns now (v76); Rolls/Pallet and Pallets/Container
            # are too (v76.1), so the old "Stuffing —" note for Strap lines is
            # gone -- it would just repeat these columns.
            spec_note = line.get("spec_note")
            if spec_note:
                cell = ws.cell(row=r, column=2, value=spec_note)
                cell.font = stuffing_font
                ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=11)
                r += 1
        return r, num

    stretch_lines = [l for l in lines if l.get("product_line") not in ("pet", "pp")]
    strap_lines = [l for l in lines if l.get("product_line") in ("pet", "pp")]
    if stretch_lines and strap_lines:
        ws.cell(row=row, column=1, value="Stretch Film").font = section_font
        row += 1
        _write_header_row(row, "$/KG")
        row += 1
        row, next_num = _write_line_rows(row, stretch_lines, 1)
        row += 1
        ws.cell(row=row, column=1, value="PET / PP Strap").font = section_font
        row += 1
        _write_header_row(row, "$/Roll")
        row += 1
        row, _ = _write_line_rows(row, strap_lines, next_num)
    elif strap_lines:
        _write_header_row(row, "$/Roll")
        row += 1
        row, _ = _write_line_rows(row, strap_lines, 1)
    else:
        _write_header_row(row, "$/KG")
        row += 1
        row, _ = _write_line_rows(row, stretch_lines, 1)

    # v96 -- owner-confirmed: no totals with prices (or total weight)
    # anywhere on the Pricing screen or in the PDF/Excel exports, for either
    # Stretch Film or Strap -- this used to end with a Global Discount/FOB
    # Total/CIF Total block (matching build_pdf()'s own removal). Per-line
    # "Line Total"/"Total Qty (KG)" columns were already dropped from this
    # same sheet earlier (v76/v76.2).

    # v69 -- widened a bit (was [4, 30, 12, 12, 12, 12, 10, 10, 10, 10, 12])
    # to give the now-2-line headers room to breathe.
    # v76.1 -- 12 columns then (Qty(Pallets) dropped, Rolls/Pallet +
    # Pallets/Container added). v76.2 -- 11 columns now (Total Qty (KG)
    # dropped too).
    widths = [5, 36, 15, 17, 14, 12, 12, 12, 16, 13, 13]
    for col, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
