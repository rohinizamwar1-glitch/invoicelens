"""Step 1 of the pipeline: turn pasted invoice text into structured fields.

Two extractors work together:
  * AI extractor (LLM) - handles messy layouts.
  * Rule-based extractor (regex) - always available, so the product still works if the AI is down.

Every value the AI returns is GROUNDED: it must literally appear in the invoice text, otherwise it is
discarded (and flagged later by the logic layer). Long account numbers are masked before anything is
sent to the AI or stored.
"""
import json
import re
import urllib.error
from datetime import datetime
from decimal import Decimal, InvalidOperation

import llm_client

FIELDS = ["vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "currency",
          "subtotal", "tax", "total", "gstin", "bank_last4"]
AMOUNT_FIELDS = {"subtotal", "tax", "total"}
CRITICAL = ["vendor_name", "invoice_number", "invoice_date", "total"]
OPTIONAL = ["due_date", "subtotal", "tax", "gstin", "bank_last4"]
LABELS = {
    "vendor_name": "Vendor", "invoice_number": "Invoice number", "invoice_date": "Invoice date",
    "due_date": "Due date", "po_number": "PO number", "currency": "Currency", "subtotal": "Subtotal",
    "tax": "GST / tax", "total": "Total payable", "gstin": "Vendor GSTIN", "bank_last4": "Bank a/c (last 4)",
}

# ------------------------------------------------------------------ privacy
_ACCOUNT_RUN = re.compile(r"(?<![\d,.])\d{9,18}(?![\d,]|\.\d)")


def mask_accounts(text):
    """50100234564821 -> XXXXXXXXXX4821 (keeps only the last four digits)."""
    return _ACCOUNT_RUN.sub(lambda m: "X" * (len(m.group()) - 4) + m.group()[-4:], text)


# ------------------------------------------------------------------ parsing helpers
_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d{1,2})?)(?![\w%])(?!\s*%)")
_DATE_TOKEN = re.compile(
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}[\s\-/\.]+(?:[A-Za-z]{3,9}|\d{1,2})[\s\-/\.,]+\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})")
_DATE_FORMATS = ["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y",
                 "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"]


def to_decimal(value):
    try:
        d = Decimal(str(value).replace(",", "").replace("₹", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def parse_date(token):
    t = re.sub(r"\s+", " ", str(token).strip())
    for cand in dict.fromkeys([t, t.replace(",", ""), t.replace(".", "/"), t.replace(".", "")]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _amounts(line):
    return [d for d in (to_decimal(m) for m in _NUM.findall(line)) if d is not None]


def _lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _last_amount_on_matching_line(text, patterns, exclude=None):
    for pat in patterns:
        for ln in _lines(text):
            if re.search(pat, ln, re.I) and not (exclude and re.search(exclude, ln, re.I)):
                vals = _amounts(ln)
                if vals:
                    return vals[-1]
    return None


def _first_date_on_matching_line(text, pattern):
    for ln in _lines(text):
        if re.search(pattern, ln, re.I):
            for tok in _DATE_TOKEN.findall(ln):
                parsed = parse_date(tok)
                if parsed:
                    return parsed
    return None


# ------------------------------------------------------------------ rule-based extractor
_GSTIN_STRICT = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
_BUYER_MARK = re.compile(r"bill(?:ed)?\s*to|buyer|ship(?:ped)?\s*to|customer|consignee", re.I)


def _vendor(text):
    m = re.search(r"^\s*(?:vendor|supplier|sold\s*by|billed\s*by|from)\s*[:\-]\s*(.+)$", text, re.I | re.M)
    if m:
        return m.group(1).strip()[:80]
    for ln in _lines(text):
        s = ln.strip(" \t*#-_=|")
        if len(s) < 3 or not re.search(r"[A-Za-z]{3}", s):
            continue
        if re.match(r"^(tax\s*)?invoice\b|^bill\s*of\s*supply|^original|^duplicate|^page\b", s, re.I):
            continue
        if len(s.split()) > 8 or re.search(r"[,!?]", s):
            return None  # reads like a sentence, not a company name
        return s[:80]
    return None


def _invoice_number(text):
    for m in re.finditer(r"(?:invoice|inv|bill)\s*(?:no\.?|number|num|#)\s*[:\-#.]?\s*([A-Za-z0-9][A-Za-z0-9\-/_.]{2,30})", text, re.I):
        val = m.group(1).rstrip(".-/_")
        if re.search(r"\d", val):
            return val
    return None


def _po_number(text):
    for m in re.finditer(r"(?:\bP\.O\.|\bPO\b|purchase\s*order)\s*(?:no\.?|number|num|ref(?:erence)?|#)?\s*[:\-#.]?\s*([A-Za-z0-9][A-Za-z0-9\-/_]{2,20})", text, re.I):
        val = m.group(1)
        if re.search(r"\d", val):
            return val.upper()
    return None


def _gstin(text):
    cut = _BUYER_MARK.search(text)
    zone = text[:cut.start()] if cut else text  # the vendor's GSTIN sits above the "Bill To" block
    m = re.search(r"GSTIN(?:\s*/\s*UIN)?\s*(?:No\.?|Number)?\s*[:\-]?\s*([A-Z0-9]{10,16})", zone)
    if m:
        return m.group(1).upper()
    m = _GSTIN_STRICT.search(zone)
    return m.group(0) if m else None


def _tax(text):
    parts, total_line = [], None
    for ln in _lines(text):
        if re.search(r"\bGSTIN\b|GST\s*(?:No|Number|Reg)|tax\s*invoice", ln, re.I):
            continue
        m = re.search(r"\b(total\s*(?:tax|gst)|igst|cgst|sgst|utgst|gst|tax)\b", ln, re.I)
        if not m:
            continue
        vals = _amounts(ln)
        if not vals:
            continue
        if m.group(1).lower().startswith("total"):
            total_line = vals[-1]
        else:
            parts.append(vals[-1])
    if total_line is not None:
        return total_line
    return sum(parts, Decimal("0")) if parts else None


def _bank_last4(text):
    m = re.search(r"[Xx*•]{3,}\s*(\d{4})\b", text)
    return m.group(1) if m else None


def _currency(text):
    if re.search(r"₹|\bRs\.?|\bINR\b", text):
        return "INR"
    if re.search(r"\$|\bUSD\b", text):
        return "USD"
    if re.search(r"€|\bEUR\b", text):
        return "EUR"
    if re.search(r"£|\bGBP\b", text):
        return "GBP"
    return None


def extract_rules(text):
    first_date = None
    toks = _DATE_TOKEN.findall(text)
    if toks:
        first_date = parse_date(toks[0])
    return {
        "vendor_name": _vendor(text),
        "invoice_number": _invoice_number(text),
        "invoice_date": _first_date_on_matching_line(text, r"invoice\s*date|date\s*of\s*invoice|bill\s*date|inv\.?\s*date|^date\b") or first_date,
        "due_date": _first_date_on_matching_line(text, r"due\s*date|payment\s*due|due\s*on|due\s*by|pay\s*by"),
        "po_number": _po_number(text),
        "currency": _currency(text),
        "subtotal": _last_amount_on_matching_line(
            text, [r"sub\s*-?\s*total", r"taxable\s*(?:value|amount)", r"amount\s*before\s*tax", r"total\s*before\s*tax",
                   r"basic\s*(?:amount|value)", r"net\s*amount"]),
        "tax": _tax(text),
        "total": _last_amount_on_matching_line(
            text, [r"grand\s*total", r"total\s*(?:amount\s*)?(?:payable|due)", r"(?:amount|net)\s*(?:payable|due)",
                   r"invoice\s*(?:total|value|amount)", r"balance\s*due",
                   r"^\W*total\b(?!\s*(?:tax|gst|igst|cgst|sgst|qty|quantity))"],
            exclude=r"sub\s*-?\s*total"),
        "gstin": _gstin(text),
        "bank_last4": _bank_last4(text),
    }


# ------------------------------------------------------------------ AI extractor
SYSTEM_PROMPT = """You extract fields from a supplier invoice for an accounts-payable team.
Return ONLY a JSON object with exactly these keys:
vendor_name, invoice_number, invoice_date (YYYY-MM-DD), due_date (YYYY-MM-DD), po_number,
currency (ISO code like INR), subtotal, tax (total GST/tax amount), total (final amount payable),
vendor_gstin (the SELLER's GSTIN, not the buyer's), bank_last4 (last 4 digits of the bank account, digits only),
description (at most 15 words about what was bought).
Rules:
- Copy values exactly as written in the invoice. Use null for anything that is not clearly present.
- NEVER calculate, guess or infer a missing value. If a number is not written in the invoice, return null.
- Amounts are plain numbers: no currency symbol, no commas.
- Account numbers are already masked; only the last four digits are visible."""


def _parse_json(text):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def _alnum(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def ground(ai, text):
    """Keep only AI values that literally appear in the invoice. Returns (kept, dropped_field_names)."""
    nums = {d for d in (to_decimal(m) for m in _NUM.findall(text)) if d is not None}
    pair_sums = set()
    small = list(nums)[:150]
    for i, a in enumerate(small):
        for b in small[i:]:
            pair_sums.add(a + b)
    dates = {parse_date(t) for t in _DATE_TOKEN.findall(text)} - {None}
    flat = _alnum(text)
    kept, dropped = {}, []
    for field in FIELDS:
        raw = ai.get("vendor_gstin" if field == "gstin" else field)
        if raw in (None, "", "null"):
            continue
        ok, value = False, None
        if field in AMOUNT_FIELDS:
            value = to_decimal(raw)
            ok = value is not None and (value in nums or (field == "tax" and value in pair_sums))
        elif field in ("invoice_date", "due_date"):
            value = parse_date(raw)
            ok = value is not None and value in dates
        elif field == "bank_last4":
            value = re.sub(r"\D", "", str(raw))
            ok = len(value) == 4 and value in text
        elif field == "currency":
            value = str(raw).upper().strip()[:3]
            ok = bool(re.fullmatch(r"[A-Z]{3}", value))
        else:
            value = str(raw).strip()
            ok = len(_alnum(value)) >= 2 and _alnum(value) in flat
            if field in ("gstin", "po_number"):
                value = value.upper()
        if ok:
            kept[field] = value
        else:
            dropped.append(field)
    return kept, dropped


def confidence(fields):
    crit = sum(1 for k in CRITICAL if fields.get(k) is not None)
    opt = sum(1 for k in OPTIONAL if fields.get(k) is not None)
    return round(70 * crit / len(CRITICAL) + 30 * opt / len(OPTIONAL))


def extract(text, use_ai=True, llm=llm_client.call_llm):
    """Full extraction. `text` must already be masked. Returns a dict with fields, sources and metadata."""
    cfg = llm_client.config()
    meta = {"method": "rules", "ai_status": "disabled" if not use_ai else "unavailable", "ai_error": None,
            "latency_ms": None, "tokens_in": None, "tokens_out": None, "model": None}
    fields = {k: None for k in FIELDS}
    sources = {}
    dropped, description = [], None

    if use_ai and cfg["key"]:
        import time
        started = time.monotonic()
        try:
            reply, usage = llm(SYSTEM_PROMPT, text, cfg)
            ai = _parse_json(reply)
            kept, dropped = ground(ai, text)
            for k, v in kept.items():
                fields[k], sources[k] = v, "ai"
            description = str(ai.get("description") or "").strip()[:140] or None
            meta.update(method="ai", ai_status="used", model=cfg["model"],
                        latency_ms=int((time.monotonic() - started) * 1000),
                        tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"))
        except urllib.error.HTTPError as exc:
            meta.update(ai_status="failed", ai_error=f"AI service returned HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError) as exc:
            meta.update(ai_status="failed", ai_error=f"AI service unreachable ({exc.__class__.__name__})")
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            meta.update(ai_status="failed", ai_error="AI reply could not be parsed")
    elif use_ai:
        meta["ai_error"] = "LLM_API_KEY is not set"

    rules = extract_rules(text)
    for k in FIELDS:  # rules fill whatever the AI did not return
        if fields[k] is None and rules.get(k) is not None:
            fields[k], sources[k] = rules[k], "rules"
    if fields["currency"] is None:
        fields["currency"], sources["currency"] = "INR", "assumed"

    return {"fields": fields, "sources": sources, "ungrounded": dropped, "description": description,
            "confidence": confidence(fields), "meta": meta}
