"""Smart logic layer. Runs AFTER extraction (AI generation -> logic layer -> database).

Every extracted invoice is run through the same rule set. Each rule either passes, is skipped (not enough
data) or produces a flag with a severity, points, evidence and a suggested action. Points add up to a
risk score (0-100) -> risk level -> decision (Approve / Needs review / Reject) -> approver by amount.

The rules read vendor master data, purchase orders, department budgets and earlier invoices through a
`lookup` object, so the engine itself stays pure and easy to test.
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from textutil import gstin_valid, inr, norm_name

SEVERITY_POINTS = {"critical": 50, "high": 25, "medium": 15, "low": 5}
REVIEW_THRESHOLD = 15      # score at/above this -> a human must look
REJECT_THRESHOLD = 70      # score at/above this -> recommend rejection
AUTO_APPROVE_LIMIT = Decimal("25000")
PO_REQUIRED_ABOVE = Decimal("50000")
GST_SLABS = [Decimal(x) for x in ("5", "12", "18", "28")]
APPROVAL_TIERS = [
    (Decimal("25000"), "Finance Executive"),
    (Decimal("100000"), "Finance Manager"),
    (Decimal("500000"), "Finance Head"),
    (None, "CFO"),
]
RULE_CATALOGUE = {  # id -> (title, default severity), used by the dashboard and the docs
    "EXTRACT_MISSING": ("Key invoice fields missing", "high"),
    "UNGROUNDED": ("AI value not found in invoice text", "medium"),
    "VENDOR_BLOCKED": ("Vendor is blocked", "critical"),
    "VENDOR_NEW": ("Vendor not in vendor master", "medium"),
    "DUP_EXACT": ("Duplicate invoice number", "critical"),
    "DUP_AMOUNT": ("Same amount from same vendor recently", "high"),
    "BANK_CHANGED": ("Bank account differs from vendor master", "critical"),
    "GSTIN_MISSING": ("Tax charged but no vendor GSTIN", "high"),
    "GSTIN_INVALID": ("GSTIN fails format / checksum", "high"),
    "GSTIN_MISMATCH": ("GSTIN differs from vendor master", "high"),
    "TAX_MATH": ("Subtotal + tax does not equal total", "high"),
    "TAX_RATE": ("Effective GST rate is not a standard slab", "medium"),
    "PO_MISSING": ("No PO on a larger invoice", "medium"),
    "PO_UNKNOWN": ("PO number not found", "high"),
    "PO_VENDOR": ("PO belongs to a different vendor", "high"),
    "PO_EXCEEDED": ("Invoice exceeds remaining PO value", "high"),
    "BUDGET": ("Department monthly budget exceeded", "medium"),
    "DATE_ORDER": ("Due date is before invoice date", "high"),
    "DATE_FUTURE": ("Invoice is dated in the future", "medium"),
    "DATE_STALE": ("Invoice is over 90 days old", "low"),
    "CURRENCY": ("Not an INR invoice", "medium"),
    "ROUND_AMOUNT": ("Perfectly round large amount", "low"),
    "HIGH_VALUE": ("High-value invoice", "low"),
}


def approver_for(total, decision):
    names = [n for _, n in APPROVAL_TIERS]
    idx = 1  # unknown amount -> Finance Manager
    if total is not None:
        for i, (limit, _) in enumerate(APPROVAL_TIERS):
            if limit is None or total <= limit:
                idx = i
                break
    if decision == "Needs review":
        idx = max(idx, 1)  # a risky invoice always gets at least a manager's eyes
    return names[idx]


def risk_level(score, hits):
    level = "Critical" if score >= 70 else "High" if score >= 40 else "Medium" if score >= 15 else "Low"
    if any(h["severity"] == "critical" for h in hits) and level in ("Low", "Medium"):
        level = "High"
    return level


def _d(value):
    return None if value is None else Decimal(str(value))


def evaluate(x, ctx, lookup):
    """x: extraction result dict; ctx: {'department','po_hint','today'}; lookup: data-access object."""
    f = x["fields"]
    today = ctx.get("today") or date.today()
    total, subtotal, tax = _d(f.get("total")), _d(f.get("subtotal")), _d(f.get("tax"))
    inv_date = datetime.strptime(f["invoice_date"], "%Y-%m-%d").date() if f.get("invoice_date") else None
    due_date = datetime.strptime(f["due_date"], "%Y-%m-%d").date() if f.get("due_date") else None
    hits, checks = [], []

    def run(rid, skip=None, hit=None):
        title = RULE_CATALOGUE[rid][0]
        if skip:
            checks.append({"id": rid, "title": title, "status": "skip", "why": skip})
        elif hit:
            sev = hit.pop("severity", RULE_CATALOGUE[rid][1])
            h = {"id": rid, "title": title, "severity": sev, "points": hit.pop("points", SEVERITY_POINTS[sev]),
                 "forces": hit.pop("forces", None), **hit}
            hits.append(h)
            checks.append({"id": rid, "title": title, "status": "hit", "why": h["detail"]})
        else:
            checks.append({"id": rid, "title": title, "status": "pass", "why": ""})

    # 1. Did we even get the basics?
    missing = [label for key, label in (("vendor_name", "vendor"), ("invoice_number", "invoice number"),
                                         ("invoice_date", "invoice date"), ("total", "total amount")) if not f.get(key)]
    run("EXTRACT_MISSING", hit={
        "detail": "Could not read: " + ", ".join(missing) + ".",
        "action": "Ask the vendor for a clearer copy or enter the details manually before any decision.",
        "forces": "review"} if missing else None)
    run("UNGROUNDED", hit={
        "detail": "The AI returned " + ", ".join(x["ungrounded"]) + " but it is not written in the invoice, so it was discarded.",
        "action": "Check those fields against the original invoice."} if x.get("ungrounded") else None)

    # 2. Vendor checks
    vendor = lookup.vendor(f["vendor_name"]) if f.get("vendor_name") else None
    vkey = norm_name(vendor["name"] if vendor else f.get("vendor_name"))
    if not f.get("vendor_name"):
        run("VENDOR_BLOCKED", skip="vendor unknown")
        run("VENDOR_NEW", skip="vendor unknown")
    else:
        run("VENDOR_BLOCKED", hit={
            "detail": f"{vendor['name']} is marked as blocked in the vendor master.",
            "action": "Do not pay. Report to the finance head and check why the vendor was blocked.",
            "forces": "reject"} if vendor and vendor["status"] == "blocked" else None)
        run("VENDOR_NEW", hit={
            "detail": f"'{f['vendor_name']}' is not in the vendor master.",
            "action": "Complete vendor onboarding (GST certificate, bank proof, KYC) before paying."} if not vendor else None)

    # 3. Duplicates
    if not (f.get("invoice_number") and vkey):
        run("DUP_EXACT", skip="needs vendor and invoice number")
    else:
        prior = lookup.invoices_by_number(vkey, f["invoice_number"])
        run("DUP_EXACT", hit={
            "detail": f"Invoice {f['invoice_number']} from this vendor was already submitted (#{prior[0]['id']}, {inr(prior[0]['total'])}).",
            "action": "Reject as duplicate. Pay only the original invoice.", "forces": "reject"} if prior else None)
    if total is None or not vkey:
        run("DUP_AMOUNT", skip="needs vendor and total")
    else:
        near = [p for p in lookup.same_amount(vkey, total, (inv_date or today).isoformat())
                if p["invoice_number"] != f.get("invoice_number")]
        run("DUP_AMOUNT", hit={
            "detail": f"{inr(total)} was already invoiced by this vendor within 30 days (#{near[0]['id']}, invoice {near[0]['invoice_number']}).",
            "action": "Confirm this is a different supply and not a re-numbered duplicate."} if near else None)

    # 4. Bank and tax identity (classic payment-fraud signals)
    if vendor and vendor.get("bank_last4") and f.get("bank_last4"):
        run("BANK_CHANGED", hit={
            "detail": f"Invoice shows account ending {f['bank_last4']} but the vendor master has ending {vendor['bank_last4']}.",
            "action": "Hold payment. Call the vendor on a number from the master (not from the invoice) to confirm the change.",
            "forces": "review"} if f["bank_last4"] != vendor["bank_last4"] else None)
    else:
        run("BANK_CHANGED", skip="no bank details to compare")
    gstin = (f.get("gstin") or "").upper()
    if tax is not None and tax > 0 and not gstin:
        run("GSTIN_MISSING", hit={"detail": f"{inr(tax)} GST is charged but no vendor GSTIN was found.",
                                  "action": "Request a valid tax invoice with the vendor GSTIN; input credit depends on it."})
    else:
        run("GSTIN_MISSING")
    if not gstin:
        run("GSTIN_INVALID", skip="no GSTIN on invoice")
    else:
        run("GSTIN_INVALID", hit={"detail": f"GSTIN {gstin} fails the format / checksum test, so it may be mistyped or fake.",
                                  "action": "Verify the GSTIN on the GST portal before paying."} if not gstin_valid(gstin) else None)
    if vendor and vendor.get("gstin") and gstin:
        run("GSTIN_MISMATCH", hit={"detail": f"Invoice GSTIN {gstin} differs from the vendor master ({vendor['gstin']}).",
                                   "action": "Confirm the legal entity before paying."} if gstin != vendor["gstin"].upper() else None)
    else:
        run("GSTIN_MISMATCH", skip="nothing to compare")

    # 5. Arithmetic
    if subtotal is None or tax is None or total is None:
        run("TAX_MATH", skip="needs subtotal, tax and total")
    else:
        diff = subtotal + tax - total
        run("TAX_MATH", hit={
            "detail": f"{inr(subtotal)} + {inr(tax)} = {inr(subtotal + tax)}, but the invoice total is {inr(total)} (difference {inr(abs(diff))}).",
            "action": "Ask the vendor for a corrected invoice."} if abs(diff) > Decimal("1") else None)
    if subtotal is None or tax is None or subtotal <= 0:
        run("TAX_RATE", skip="needs subtotal and tax")
    else:
        rate = tax / subtotal * 100
        ok = tax == 0 or any(abs(rate - s) <= Decimal("0.5") for s in GST_SLABS)
        run("TAX_RATE", hit={"detail": f"Effective GST rate is {rate:.1f}%; standard slabs are 5, 12, 18 and 28%.",
                             "action": "Check the HSN/SAC code and rate with the vendor."} if not ok else None)

    # 6. Purchase order
    po_number = f.get("po_number") or ctx.get("po_hint")
    if not po_number:
        if total is None:
            run("PO_MISSING", skip="total unknown")
        else:
            run("PO_MISSING", hit={"detail": f"{inr(total)} is above {inr(PO_REQUIRED_ABOVE)} but no PO number was found.",
                                   "action": "Ask the requester for the PO or retro-approve through the finance head."} if total > PO_REQUIRED_ABOVE else None)
        for rid in ("PO_UNKNOWN", "PO_VENDOR", "PO_EXCEEDED"):
            run(rid, skip="no PO on invoice")
    else:
        run("PO_MISSING")
        po = lookup.po(po_number)
        run("PO_UNKNOWN", hit={"detail": f"PO {po_number} is not in the system.",
                               "action": "Ask the requester for the correct PO."} if not po else None)
        if not po:
            run("PO_VENDOR", skip="PO not found")
            run("PO_EXCEEDED", skip="PO not found")
        else:
            from textutil import same_company
            run("PO_VENDOR", hit={"detail": f"PO {po_number} was raised for {po['vendor_name']}, not {f.get('vendor_name') or 'this vendor'}.",
                                  "action": "Check whether the invoice is against the right PO."}
                if f.get("vendor_name") and not same_company(po["vendor_name"], f["vendor_name"]) else None)
            if total is None:
                run("PO_EXCEEDED", skip="total unknown")
            else:
                remaining = Decimal(str(po["amount"])) - lookup.po_invoiced(po_number)
                tolerance = max(Decimal("1"), Decimal(str(po["amount"])) * Decimal("0.02"))
                run("PO_EXCEEDED", hit={
                    "detail": f"Invoice {inr(total)} vs {inr(remaining)} left on PO {po_number} (PO value {inr(po['amount'])}).",
                    "action": "Get a PO amendment approved before paying the extra amount."} if total > remaining + tolerance else None)

    # 7. Budget
    dept = ctx.get("department")
    budget = lookup.budget(dept) if dept else None
    if budget is None or total is None:
        run("BUDGET", skip="no budget or total")
    else:
        period = (inv_date or today).strftime("%Y-%m")
        spent = lookup.dept_spend(dept, period)
        run("BUDGET", hit={
            "detail": f"{dept} has spent {inr(spent)} of its {inr(budget)} monthly budget for {period}; this invoice takes it to {inr(spent + total)}.",
            "action": "Needs budget-owner sign-off or a reallocation."} if spent + total > budget else None)

    # 8. Dates and amount pattern
    if inv_date and due_date:
        run("DATE_ORDER", hit={"detail": f"Due date {due_date:%d %b %Y} is before the invoice date {inv_date:%d %b %Y}.",
                               "action": "Ask the vendor to correct the dates."} if due_date < inv_date else None)
    else:
        run("DATE_ORDER", skip="needs both dates")
    if inv_date:
        run("DATE_FUTURE", hit={"detail": f"Invoice is dated {inv_date:%d %b %Y}, after today.",
                                "action": "Hold until the invoice date; check for a typo."} if inv_date > today else None)
        run("DATE_STALE", hit={"detail": f"Invoice is {(today - inv_date).days} days old.",
                               "action": "Check for late-payment penalties and whether the supply was recorded."} if inv_date < today - timedelta(days=90) else None)
    else:
        run("DATE_FUTURE", skip="no invoice date")
        run("DATE_STALE", skip="no invoice date")
    run("CURRENCY", hit={"detail": f"Invoice is in {f['currency']}, not INR.",
                         "action": "Check FX rate and whether foreign-currency payments are approved."}
        if f.get("currency") and f["currency"] != "INR" else None)
    run("ROUND_AMOUNT", hit={"detail": f"{inr(total)} is a perfectly round amount.",
                             "action": "Look at the line items; round totals can signal estimates."}
        if total is not None and total >= 50000 and total % 10000 == 0 else None)
    run("HIGH_VALUE", hit={"detail": f"{inr(total)} is above {inr(APPROVAL_TIERS[2][0])}.",
                           "action": "Route to the CFO."} if total is not None and total > APPROVAL_TIERS[2][0] else None)

    # ---- score -> level -> decision -> approver
    hits.sort(key=lambda h: -h["points"])
    score = min(100, sum(h["points"] for h in hits))
    level = risk_level(score, hits)
    if any(h["forces"] == "reject" for h in hits) or score >= REJECT_THRESHOLD:
        decision = "Reject"
    elif any(h["forces"] == "review" for h in hits) or score >= REVIEW_THRESHOLD:
        decision = "Needs review"
    else:
        decision = "Approve"
    approver = None if decision == "Reject" else approver_for(total, decision)
    auto = decision == "Approve" and total is not None and total <= AUTO_APPROVE_LIMIT

    passed = sum(1 for c in checks if c["status"] == "pass")
    vendor_label = f.get("vendor_name") or "the vendor"
    number = f.get("invoice_number") or "(no number)"
    if decision == "Approve":
        note = (f"Recommend approval. {vendor_label} invoice {number} for {inr(total)}: {passed} checks passed and "
                f"no risk flags. " + ("Within the auto-approval limit." if auto else f"Route to {approver}."))
        next_action = "Auto-approved; schedule payment on the due date." if auto else f"Send to {approver} for approval."
    elif decision == "Needs review":
        issues = "; ".join(h["title"][0].lower() + h["title"][1:] for h in hits[:3])
        note = (f"Hold for review. {vendor_label} invoice {number} for {inr(total)} scored {score}/100 ({level}). "
                f"Main concerns: {issues}. Needs {approver} sign-off once resolved.")
        next_action = hits[0]["action"] if hits else "Review manually."
    else:
        reasons = "; ".join(h["title"][0].lower() + h["title"][1:] for h in hits[:3])
        note = f"Recommend rejection. {vendor_label} invoice {number} for {inr(total)} scored {score}/100 ({level}): {reasons}."
        next_action = hits[0]["action"] if hits else "Reject."

    return {"score": score, "level": level, "decision": decision, "approver": approver, "auto_approved": auto,
            "hits": hits, "checks": checks, "passed": passed, "note": note, "next_action": next_action}
