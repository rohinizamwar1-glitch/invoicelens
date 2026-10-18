"""Checks the submission form before anything is sent to the pipeline."""
import re

MIN_TEXT, MAX_TEXT = 40, 6000


def validate_submission(form, allowed_departments):
    errors = []
    who = (form.get("submitted_by") or "").strip()
    dept = (form.get("department") or "").strip()
    text = (form.get("invoice_text") or "").replace("\r\n", "\n").strip()
    po = (form.get("po_hint") or "").strip().upper()

    if not who:
        errors.append("Tell us who is submitting this invoice.")
    elif len(who) > 80:
        errors.append("Name is too long (80 characters max).")
    if dept not in allowed_departments:
        errors.append("Choose the department that will bear this cost.")
    if len(text) < MIN_TEXT:
        errors.append(f"Paste the invoice text (at least {MIN_TEXT} characters).")
    elif len(text) > MAX_TEXT:
        errors.append(f"Invoice text is too long ({len(text)} characters, max {MAX_TEXT}). Paste only the invoice itself.")
    if po and not re.fullmatch(r"[A-Z0-9][A-Z0-9\-/]{2,24}", po):
        errors.append("PO number looks wrong. Use letters, numbers and dashes, like PO-1042.")
    return {"submitted_by": who, "department": dept, "invoice_text": text, "po_hint": po or None}, errors
