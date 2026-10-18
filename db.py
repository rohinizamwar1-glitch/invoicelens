"""SQLite storage: schema, master data, the lookup object used by the risk engine, and dashboard queries."""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

from textutil import norm_name, same_company

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "invoicelens.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    vendor_key TEXT NOT NULL UNIQUE,
    gstin TEXT,
    bank_last4 TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','blocked'))
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number TEXT PRIMARY KEY,
    vendor_name TEXT NOT NULL,
    amount REAL NOT NULL,
    department TEXT
);
CREATE TABLE IF NOT EXISTS departments (
    name TEXT PRIMARY KEY,
    monthly_budget REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    department TEXT NOT NULL,
    source_text TEXT NOT NULL,
    vendor_name TEXT, vendor_key TEXT, invoice_number TEXT,
    invoice_date TEXT, due_date TEXT, po_number TEXT, currency TEXT,
    subtotal REAL, tax REAL, total REAL, gstin TEXT, bank_last4 TEXT,
    period TEXT,
    extracted_json TEXT, extraction_method TEXT, extraction_confidence INTEGER,
    ai_status TEXT, ai_error TEXT, latency_ms INTEGER, tokens_in INTEGER, tokens_out INTEGER, model TEXT,
    risk_score INTEGER, risk_level TEXT, decision TEXT, approver TEXT, result_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    decided_by TEXT, decided_at TEXT, reviewer_note TEXT, overridden INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inv_vendor ON invoices(vendor_key, invoice_number);
CREATE INDEX IF NOT EXISTS idx_inv_status ON invoices(status);
"""

SEED_VENDORS = [
    ("Kiran Print & Pack Pvt Ltd", "27AABCK4417M1ZD", "4821", "active"),
    ("Orbit Logistics Pvt Ltd", "29AACCO8821Q1ZO", "7730", "active"),
    ("Lotus Creative Studio", "27AAFFL2190B1ZN", "1156", "active"),
    ("Nimbus Cloud Services Pvt Ltd", "29AAGCN5530D1ZK", "9042", "active"),
    ("Vega Office Supplies", "27AAJFV3391P1ZB", "3318", "active"),
    ("Shree Ganesh Traders", "27AAHFS6604K1ZW", "6604", "blocked"),
]
SEED_POS = [
    ("PO-1042", "Kiran Print & Pack Pvt Ltd", 118000, "Marketing"),
    ("PO-1050", "Orbit Logistics Pvt Ltd", 300000, "Operations"),
    ("PO-1061", "Nimbus Cloud Services Pvt Ltd", 177000, "Technology"),
    ("PO-1066", "Lotus Creative Studio", 236000, "Marketing"),
    ("PO-1070", "Vega Office Supplies", 40000, "Admin"),
]
SEED_DEPARTMENTS = [("Marketing", 600000), ("Operations", 800000), ("Technology", 300000),
                    ("Admin", 100000), ("Finance", 200000)]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0:
            for name, gstin, bank, status in SEED_VENDORS:
                c.execute("INSERT INTO vendors(name,vendor_key,gstin,bank_last4,status) VALUES (?,?,?,?,?)",
                          (name, norm_name(name), gstin, bank, status))
            c.executemany("INSERT INTO purchase_orders VALUES (?,?,?,?)", SEED_POS)
            c.executemany("INSERT INTO departments VALUES (?,?)", SEED_DEPARTMENTS)


class DbLookup:
    """Gives the risk engine read access to master data and invoice history."""

    def __init__(self, conn):
        self.c = conn

    def vendor(self, name):
        for row in self.c.execute("SELECT * FROM vendors"):
            if same_company(row["name"], name):
                return dict(row)
        return None

    def invoices_by_number(self, vendor_key, invoice_number):
        rows = self.c.execute("SELECT id,total,invoice_number FROM invoices WHERE vendor_key=? AND lower(invoice_number)=lower(?)",
                              (vendor_key, invoice_number)).fetchall()
        return [dict(r) for r in rows]

    def same_amount(self, vendor_key, total, date_iso):
        centre = datetime.strptime(date_iso, "%Y-%m-%d").date()
        lo, hi = (centre - timedelta(days=30)).isoformat(), (centre + timedelta(days=30)).isoformat()
        rows = self.c.execute("SELECT id,invoice_number FROM invoices WHERE vendor_key=? AND ABS(total-?)<0.5 "
                              "AND invoice_date BETWEEN ? AND ? AND status!='rejected'", (vendor_key, float(total), lo, hi))
        return [dict(r) for r in rows]

    def po(self, po_number):
        row = self.c.execute("SELECT * FROM purchase_orders WHERE upper(po_number)=upper(?)", (po_number,)).fetchone()
        return dict(row) if row else None

    def po_invoiced(self, po_number):
        v = self.c.execute("SELECT COALESCE(SUM(total),0) FROM invoices WHERE upper(po_number)=upper(?) AND status!='rejected'",
                           (po_number,)).fetchone()[0]
        return Decimal(str(v))

    def budget(self, dept):
        row = self.c.execute("SELECT monthly_budget FROM departments WHERE name=?", (dept,)).fetchone()
        return Decimal(str(row[0])) if row else None

    def dept_spend(self, dept, period):
        v = self.c.execute("SELECT COALESCE(SUM(total),0) FROM invoices WHERE department=? AND period=? AND status!='rejected'",
                           (dept, period)).fetchone()[0]
        return Decimal(str(v))


def departments(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM departments ORDER BY name")]


def get_invoice(conn, invoice_id):
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["extracted"] = json.loads(d["extracted_json"] or "{}")
    d["result"] = json.loads(d["result_json"] or "{}")
    return d


def list_invoices(conn, status=None, decision=None, limit=200):
    sql, args = "SELECT * FROM invoices WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if decision:
        sql += " AND decision=?"; args.append(decision)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def stats(conn):
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    total = one("SELECT COUNT(*) FROM invoices")
    out = {
        "total": total,
        "pending": one("SELECT COUNT(*) FROM invoices WHERE status='pending'"),
        "approved": one("SELECT COUNT(*) FROM invoices WHERE status='approved'"),
        "rejected": one("SELECT COUNT(*) FROM invoices WHERE status='rejected'"),
        "auto": one("SELECT COUNT(*) FROM invoices WHERE decided_by='System (auto)'"),
        "overridden": one("SELECT COUNT(*) FROM invoices WHERE overridden=1"),
        "value_pending": one("SELECT COALESCE(SUM(total),0) FROM invoices WHERE status='pending'"),
        "value_blocked": one("SELECT COALESCE(SUM(total),0) FROM invoices WHERE status='rejected'"),
        "avg_confidence": one("SELECT CAST(COALESCE(ROUND(AVG(extraction_confidence)),0) AS INTEGER) FROM invoices"),
        "ai_used": one("SELECT COUNT(*) FROM invoices WHERE extraction_method='ai'"),
        "ai_failed": one("SELECT COUNT(*) FROM invoices WHERE ai_status='failed'"),
        "tokens": one("SELECT COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) FROM invoices"),
        "avg_latency": one("SELECT CAST(COALESCE(ROUND(AVG(latency_ms)),0) AS INTEGER) FROM invoices WHERE latency_ms IS NOT NULL"),
    }
    out["by_decision"] = {r[0]: r[1] for r in conn.execute("SELECT decision, COUNT(*) FROM invoices GROUP BY decision")}
    out["by_level"] = {r[0]: r[1] for r in conn.execute("SELECT risk_level, COUNT(*) FROM invoices GROUP BY risk_level")}
    rules = {}
    for (rj,) in conn.execute("SELECT result_json FROM invoices"):
        for h in json.loads(rj or "{}").get("hits", []):
            rules.setdefault(h["id"], {"title": h["title"], "count": 0})["count"] += 1
    out["top_rules"] = sorted(({"id": k, **v} for k, v in rules.items()), key=lambda r: -r["count"])[:6]
    out["by_department"] = [dict(r) for r in conn.execute(
        "SELECT i.department, COUNT(*) n, COALESCE(SUM(CASE WHEN i.status!='rejected' THEN i.total END),0) spend, "
        "d.monthly_budget budget FROM invoices i LEFT JOIN departments d ON d.name=i.department GROUP BY i.department")]
    return out
