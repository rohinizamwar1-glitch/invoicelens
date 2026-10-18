"""InvoiceLens: reads a supplier invoice, checks it against company records and recommends approve / review / reject.

Flow: form -> validation -> mask account numbers -> extraction (AI + rules) -> logic layer (23 risk rules)
      -> SQLite -> decision report -> admin dashboard (human approve / reject / override)
"""
import hmac
from contextlib import contextmanager
import json
import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

import db
import extraction
import pipeline
from llm_client import config as llm_config
from risk_engine import RULE_CATALOGUE, SEVERITY_POINTS, REVIEW_THRESHOLD, REJECT_THRESHOLD, AUTO_APPROVE_LIMIT, APPROVAL_TIERS
from textutil import GSTIN_RE, gstin_valid, inr, norm_name
from validation import validate_submission

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
db.init_db()

app.add_template_filter(inr, "inr")


@app.template_filter("dmy")
def dmy(value):
    from datetime import datetime
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except (TypeError, ValueError):
        return "—"


@app.template_filter("when")
def when(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(value).strftime("%d %b, %H:%M")
    except (TypeError, ValueError):
        return "—"


@app.context_processor
def inject():
    return {"llm": llm_config(), "is_admin": bool(session.get("admin"))}


def samples():
    return json.loads((BASE_DIR / "sample_data" / "invoices.json").read_text(encoding="utf-8"))


@contextmanager
def conn():
    c = db.connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


@app.route("/")
def index():
    form = {"submitted_by": "", "department": "", "po_hint": "", "invoice_text": ""}
    pick = request.args.get("sample")
    if pick:
        s = next((s for s in samples() if s["id"] == pick), None)
        if s:
            form = {"submitted_by": s["submitted_by"], "department": s["department"], "po_hint": "", "invoice_text": s["text"]}
    with conn() as c:
        depts = db.departments(c)
    return render_template("index.html", form=form, errors=[], departments=depts, samples=samples(), picked=pick)


@app.route("/submit", methods=["POST"])
def submit():
    with conn() as c:
        depts = db.departments(c)
        data, errors = validate_submission(request.form, depts)
        if errors:
            return render_template("index.html", form={**data, "invoice_text": request.form.get("invoice_text", "")},
                                   errors=errors, departments=depts, samples=samples(), picked=None), 422
        try:
            invoice_id = pipeline.process(c, data["submitted_by"], data["department"], data["invoice_text"], data["po_hint"])
        except Exception:
            app.logger.exception("Pipeline failed")
            return render_template("index.html", form=data, errors=["Something went wrong while processing this invoice. Your text is kept below; please try again."],
                                   departments=depts, samples=samples(), picked=None), 500
    return redirect(url_for("invoice", invoice_id=invoice_id))


@app.route("/invoices")
def invoices():
    status = request.args.get("status") or None
    decision = request.args.get("decision") or None
    with conn() as c:
        rows = db.list_invoices(c, status, decision)
    return render_template("invoices.html", rows=rows, status=status, decision=decision)


@app.route("/invoices/<int:invoice_id>")
def invoice(invoice_id):
    with conn() as c:
        inv = db.get_invoice(c, invoice_id)
    if not inv:
        abort(404)
    return render_template("invoice.html", i=inv, r=inv["result"], x=inv["extracted"], f=inv["extracted"]["fields"],
                           labels=extraction.LABELS)


# ---------- admin ----------
def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    configured = os.getenv("ADMIN_PASSWORD", "")
    error = None
    if request.method == "POST":
        if configured and hmac.compare_digest(request.form.get("password", ""), configured):
            session["admin"] = True
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin"))
        error = "Incorrect password."
    return render_template("admin_login.html", configured=bool(configured), error=error)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin():
    with conn() as c:
        s = db.stats(c)
        queue = db.list_invoices(c, status="pending")
        recent = db.list_invoices(c, limit=8)
    return render_template("admin.html", s=s, queue=queue, recent=recent, limit=AUTO_APPROVE_LIMIT,
                           review=REVIEW_THRESHOLD, reject=REJECT_THRESHOLD)


@app.route("/invoices/<int:invoice_id>/action", methods=["POST"])
@admin_required
def invoice_action(invoice_id):
    with conn() as c:
        ok, msg = pipeline.decide(c, invoice_id, request.form.get("action", ""), request.form.get("reviewer", "").strip() or "Admin",
                                  request.form.get("note", ""))
    flash(msg)
    nxt = request.form.get("next") or ""
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("invoice", invoice_id=invoice_id))


@app.route("/admin/master")
@admin_required
def admin_master():
    with conn() as c:
        vendors = [dict(r) for r in c.execute("SELECT * FROM vendors ORDER BY name")]
        pos = [dict(r) for r in c.execute("SELECT * FROM purchase_orders ORDER BY po_number")]
        for p in pos:
            p["invoiced"] = float(db.DbLookup(c).po_invoiced(p["po_number"]))
        depts = [dict(r) for r in c.execute("SELECT * FROM departments ORDER BY name")]
    return render_template("admin_master.html", vendors=vendors, pos=pos, depts=depts,
                           rules=RULE_CATALOGUE, points=SEVERITY_POINTS, tiers=APPROVAL_TIERS)


@app.route("/admin/vendors", methods=["POST"])
@admin_required
def add_vendor():
    name = request.form.get("name", "").strip()
    gstin = request.form.get("gstin", "").strip().upper()
    bank = request.form.get("bank_last4", "").strip()
    if not name or len(name) > 120:
        flash("Vendor name is required.")
    elif gstin and not gstin_valid(gstin):
        flash("That GSTIN is not valid (format or check digit).")
    elif bank and not (bank.isdigit() and len(bank) == 4):
        flash("Bank: enter exactly the last 4 digits.")
    else:
        try:
            with conn() as c:
                c.execute("INSERT INTO vendors(name,vendor_key,gstin,bank_last4) VALUES (?,?,?,?)",
                          (name, norm_name(name), gstin or None, bank or None))
            flash(f"Added vendor {name}.")
        except sqlite3.IntegrityError:
            flash("A vendor with that name already exists.")
    return redirect(url_for("admin_master"))


@app.route("/admin/vendors/<int:vendor_id>/toggle", methods=["POST"])
@admin_required
def toggle_vendor(vendor_id):
    with conn() as c:
        c.execute("UPDATE vendors SET status = CASE status WHEN 'active' THEN 'blocked' ELSE 'active' END WHERE id=?", (vendor_id,))
    flash("Vendor status updated. New invoices are checked against it straight away.")
    return redirect(url_for("admin_master"))


@app.route("/admin/seed-demo", methods=["POST"])
@admin_required
def seed_demo():
    with conn() as c:
        added = pipeline.seed_demo(c)
    flash("Added one earlier invoice (Kiran Print & Pack) so the duplicate check has history." if added else "Demo history is already there.")
    return redirect(url_for("admin"))


@app.errorhandler(404)
def not_found(_):
    return render_template("message.html", title="Not found", text="That page or invoice does not exist."), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
