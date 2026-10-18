"""The end-to-end flow: mask -> extract -> logic layer -> store. Also human decisions and demo seeding."""
import json
import os
from datetime import date, datetime

import db
import extraction
import risk_engine
from textutil import norm_name

SYSTEM_USER = "System (auto)"


def process(conn, submitted_by, department, text, po_hint=None, use_ai=True, today=None):
    """Run one invoice through the whole pipeline and return its stored row id."""
    today = today or date.today()
    masked = extraction.mask_accounts(text)
    x = extraction.extract(masked, use_ai=use_ai)
    f = x["fields"]
    if po_hint and not f.get("po_number"):
        f["po_number"], x["sources"]["po_number"] = po_hint.strip().upper(), "user"
    ctx = {"department": department, "po_hint": po_hint, "today": today}
    lookup = db.DbLookup(conn)
    result = risk_engine.evaluate(x, ctx, lookup)

    vendor = lookup.vendor(f["vendor_name"]) if f.get("vendor_name") else None
    vendor_key = norm_name(vendor["name"] if vendor else f.get("vendor_name"))
    status, decided_by, decided_at = "pending", None, None
    now = datetime.now().isoformat(timespec="seconds")
    if result["decision"] == "Reject":
        status, decided_by, decided_at = "rejected", SYSTEM_USER, now
    elif result["auto_approved"]:
        status, decided_by, decided_at = "approved", SYSTEM_USER, now

    period = (f.get("invoice_date") or today.isoformat())[:7]
    m = x["meta"]
    cur = conn.execute(
        """INSERT INTO invoices(created_at,submitted_by,department,source_text,vendor_name,vendor_key,invoice_number,
        invoice_date,due_date,po_number,currency,subtotal,tax,total,gstin,bank_last4,period,extracted_json,
        extraction_method,extraction_confidence,ai_status,ai_error,latency_ms,tokens_in,tokens_out,model,
        risk_score,risk_level,decision,approver,result_json,status,decided_by,decided_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now, submitted_by, department, masked, f.get("vendor_name"), vendor_key, f.get("invoice_number"),
         f.get("invoice_date"), f.get("due_date"), f.get("po_number"), f.get("currency"),
         _num(f.get("subtotal")), _num(f.get("tax")), _num(f.get("total")), f.get("gstin"), f.get("bank_last4"), period,
         json.dumps({"fields": f, "sources": x["sources"], "ungrounded": x["ungrounded"], "description": x["description"]}, default=str),
         m["method"], x["confidence"], m["ai_status"], m["ai_error"], m["latency_ms"], m["tokens_in"], m["tokens_out"], m["model"],
         result["score"], result["level"], result["decision"], result["approver"],
         json.dumps(result, default=str), status, decided_by, decided_at))
    conn.commit()
    return cur.lastrowid


def _num(v):
    return None if v is None else float(v)


def decide(conn, invoice_id, action, reviewer, note=""):
    """Human decision from the admin dashboard. Records an override when it goes against the system's recommendation."""
    row = conn.execute("SELECT decision, status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        return False, "Invoice not found."
    if action not in ("approve", "reject"):
        return False, "Unknown action."
    new_status = "approved" if action == "approve" else "rejected"
    overridden = int((new_status == "approved" and row["decision"] == "Reject") or
                     (new_status == "rejected" and row["decision"] == "Approve"))
    if overridden and not note.strip():
        return False, "Please add a note: you are going against the system's recommendation."
    conn.execute("UPDATE invoices SET status=?, decided_by=?, decided_at=?, reviewer_note=?, overridden=? WHERE id=?",
                 (new_status, reviewer, datetime.now().isoformat(timespec="seconds"), note.strip()[:500], overridden, invoice_id))
    conn.commit()
    return True, f"Invoice #{invoice_id} {new_status}."


def seed_demo(conn):
    """Adds one earlier invoice so the duplicate check has something to find. Safe to call repeatedly."""
    if conn.execute("SELECT COUNT(*) FROM invoices WHERE submitted_by='Demo data'").fetchone()[0]:
        return 0
    path = os.path.join(os.path.dirname(__file__), "sample_data", "history_seed.json")
    h = json.load(open(path, encoding="utf-8"))
    process(conn, "Demo data", h["department"], h["text"], use_ai=False, today=date(2026, 10, 14))
    return 1
