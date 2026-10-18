"""Small shared helpers: name matching, GSTIN validation, Indian rupee formatting."""
import difflib
import re
from decimal import Decimal

_SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "and", "the"}


def norm_name(name):
    """'Kiran Print & Pack Pvt. Ltd.' -> 'kiranprintpack' (so spelling variants match)."""
    s = (name or "").lower().replace("&", " and ")
    words = [w for w in re.findall(r"[a-z0-9]+", s) if w not in _SUFFIXES]
    return "".join(words)


def same_company(a, b, threshold=0.88):
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


# ---- GSTIN: 15 characters, last one is a checksum (mod-36) -----------------
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CH = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check_char(first14):
    total = 0
    for i, c in enumerate(first14):
        v = _CH.index(c) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return _CH[(36 - total % 36) % 36]


def gstin_valid(gstin):
    g = (gstin or "").strip().upper()
    return bool(GSTIN_RE.match(g)) and gstin_check_char(g[:14]) == g[14]


# ---- money ------------------------------------------------------------------
def inr(value):
    """Format with Indian digit grouping: 1180000 -> ₹11,80,000"""
    if value is None or value == "":
        return "—"
    v = Decimal(str(value))
    neg, v = v < 0, abs(v)
    whole, frac = f"{v:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        whole = head + "," + tail
    return ("-" if neg else "") + "₹" + whole + ("" if frac == "00" else "." + frac)
