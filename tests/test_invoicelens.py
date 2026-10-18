import json
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["ADMIN_PASSWORD"] = "pw-test"
os.environ.pop("LLM_API_KEY", None)

import db, extraction, pipeline, risk_engine  # noqa: E402
from textutil import gstin_valid, inr, norm_name, same_company  # noqa: E402

SAMPLES = {s["id"]: s for s in json.loads((Path(__file__).parent.parent / "sample_data" / "invoices.json").read_text(encoding="utf-8"))}
TODAY = date(2026, 10, 18)


def fresh_db():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()
    c = db.connect()
    pipeline.seed_demo(c)
    return c


def run(c, sid):
    s = SAMPLES[sid]
    return db.get_invoice(c, pipeline.process(c, s["submitted_by"], s["department"], s["text"], use_ai=False, today=TODAY))


class TextUtil(unittest.TestCase):
    def test_inr_grouping(self):
        self.assertEqual(inr(1180000), "₹11,80,000")
        self.assertEqual(inr(None), "—")

    def test_gstin(self):
        self.assertTrue(gstin_valid("27AABCK4417M1ZD"))
        self.assertFalse(gstin_valid("27AAKFZ9012E1ZX"))
        self.assertFalse(gstin_valid("BADGSTIN"))

    def test_company_matching(self):
        self.assertTrue(same_company("KIRAN PRINT & PACK PVT LTD", "Kiran Print and Pack Private Limited"))
        self.assertFalse(same_company("Orbit Logistics", "Lotus Creative Studio"))
        self.assertEqual(norm_name("The Orbit Co. Pvt Ltd"), "orbit")


class Extraction(unittest.TestCase):
    def test_masks_account_numbers(self):
        m = extraction.mask_accounts("A/c 50100234564821 IFSC HDFC0001234")
        self.assertNotIn("50100234564821", m)
        self.assertIn("4821", m)

    def test_rules_read_clean_invoice(self):
        f = extraction.extract(extraction.mask_accounts(SAMPLES["duplicate"]["text"]), use_ai=False)["fields"]
        self.assertEqual(f["invoice_number"], "KPP/2026/0412")
        self.assertEqual(Decimal(str(f["total"])), Decimal("118000"))
        self.assertEqual(f["invoice_date"], "2026-10-12")
        self.assertEqual(f["gstin"], "27AABCK4417M1ZD")  # vendor's, not the buyer's
        self.assertEqual(f["bank_last4"], "4821")

    def test_messy_text_reports_missing_fields(self):
        x = extraction.extract(SAMPLES["messy"]["text"], use_ai=False)
        self.assertIsNone(x["fields"]["total"])
        self.assertLess(x["confidence"], 20)

    def test_grounding_drops_invented_values(self):
        text = SAMPLES["small"]["text"]
        fake = lambda s, u, c: (json.dumps({"vendor_name": "VEGA OFFICE SUPPLIES", "invoice_number": "VOS-3391", "total": "99999.00",
                                            "invoice_date": "2026-10-15"}), {"prompt_tokens": 10, "completion_tokens": 5})
        os.environ["LLM_API_KEY"] = "x"
        try:
            x = extraction.extract(text, use_ai=True, llm=fake)
        finally:
            os.environ.pop("LLM_API_KEY")
        self.assertIn("total", x["ungrounded"])
        self.assertEqual(Decimal(str(x["fields"]["total"])), Decimal("8260"))  # rules recovered the true value
        self.assertEqual(x["meta"]["ai_status"], "used")

    def test_ai_failure_falls_back_to_rules(self):
        def boom(*a):
            raise ValueError("bad")
        os.environ["LLM_API_KEY"] = "x"
        try:
            x = extraction.extract(SAMPLES["small"]["text"], use_ai=True, llm=boom)
        finally:
            os.environ.pop("LLM_API_KEY")
        self.assertEqual(x["meta"]["ai_status"], "failed")
        self.assertEqual(x["fields"]["invoice_number"], "VOS-3391")


class LogicLayer(unittest.TestCase):
    def setUp(self):
        self.c = fresh_db()

    def hits(self, r):
        return {h["id"] for h in r["result"]["hits"]}

    def test_clean_invoice_is_approved_without_flags(self):
        r = run(self.c, "clean")
        self.assertEqual((r["decision"], r["risk_score"], r["approver"]), ("Approve", 0, "Finance Manager"))
        self.assertEqual(r["status"], "pending")

    def test_small_clean_invoice_is_auto_approved(self):
        r = run(self.c, "small")
        self.assertEqual((r["decision"], r["status"], r["decided_by"]), ("Approve", "approved", pipeline.SYSTEM_USER))

    def test_duplicate_is_rejected(self):
        r = run(self.c, "duplicate")
        self.assertIn("DUP_EXACT", self.hits(r))
        self.assertEqual((r["decision"], r["status"]), ("Reject", "rejected"))

    def test_bank_change_forces_review(self):
        r = run(self.c, "bank")
        self.assertEqual(self.hits(r), {"BANK_CHANGED"})
        self.assertEqual(r["decision"], "Needs review")

    def test_tax_math_error(self):
        self.assertIn("TAX_MATH", self.hits(run(self.c, "math")))

    def test_over_po(self):
        r = run(self.c, "overpo")
        self.assertIn("PO_EXCEEDED", self.hits(r))
        self.assertEqual(r["decision"], "Needs review")

    def test_new_vendor_invalid_gstin_rejected(self):
        r = run(self.c, "newvendor")
        self.assertTrue({"VENDOR_NEW", "GSTIN_INVALID", "PO_MISSING"} <= self.hits(r))
        self.assertEqual(r["decision"], "Reject")
        self.assertGreaterEqual(r["risk_score"], 70)

    def test_blocked_vendor_rejected(self):
        r = run(self.c, "blocked")
        self.assertIn("VENDOR_BLOCKED", self.hits(r))
        self.assertEqual(r["decision"], "Reject")

    def test_unreadable_invoice_needs_review(self):
        r = run(self.c, "messy")
        self.assertIn("EXTRACT_MISSING", self.hits(r))
        self.assertEqual(r["decision"], "Needs review")

    def test_approver_tiers(self):
        for total, who in ((20000, "Finance Executive"), (90000, "Finance Manager"), (400000, "Finance Head"), (900000, "CFO")):
            self.assertEqual(risk_engine.approver_for(Decimal(total), "Approve"), who)
        self.assertEqual(risk_engine.approver_for(Decimal(5000), "Needs review"), "Finance Manager")

    def test_blocking_vendor_changes_next_decision(self):
        self.c.execute("UPDATE vendors SET status='blocked' WHERE name LIKE 'Vega%'")
        self.c.commit()
        self.assertEqual(run(self.c, "small")["decision"], "Reject")

    def test_account_number_never_stored(self):
        r = run(self.c, "clean")
        self.assertNotIn("62200144897730", r["source_text"])


class HumanDecisions(unittest.TestCase):
    def setUp(self):
        self.c = fresh_db()

    def test_approve_pending(self):
        r = run(self.c, "bank")
        ok, _ = pipeline.decide(self.c, r["id"], "approve", "Admin", "Called vendor, change confirmed")
        self.assertTrue(ok)
        self.assertEqual(db.get_invoice(self.c, r["id"])["status"], "approved")

    def test_override_requires_note(self):
        r = run(self.c, "blocked")
        ok, msg = pipeline.decide(self.c, r["id"], "approve", "Admin", "")
        self.assertFalse(ok)
        ok, _ = pipeline.decide(self.c, r["id"], "approve", "Admin", "CFO exception")
        self.assertTrue(ok)
        self.assertEqual(db.get_invoice(self.c, r["id"])["overridden"], 1)

    def test_stats(self):
        for sid in ("clean", "small", "blocked"):
            run(self.c, sid)
        s = db.stats(self.c)
        self.assertEqual(s["total"], 4)  # incl. seeded history
        self.assertEqual(s["auto"], 2)  # small approved + blocked rejected by system


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as appmod
        cls.app = appmod.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        fresh_db()
        self.client = self.app.test_client()

    def login(self):
        self.client.post("/admin/login", data={"password": "pw-test"})

    def test_pages_load(self):
        for url in ("/", "/invoices", "/admin/login", "/?sample=bank"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_submit_validation(self):
        r = self.client.post("/submit", data={"submitted_by": "", "department": "Nope", "invoice_text": "short"})
        self.assertEqual(r.status_code, 422)
        self.assertIn(b"Paste the invoice text", r.data)

    def test_submit_end_to_end(self):
        s = SAMPLES["bank"]
        r = self.client.post("/submit", data={"submitted_by": s["submitted_by"], "department": s["department"], "invoice_text": s["text"]})
        self.assertEqual(r.status_code, 302)
        page = self.client.get(r.headers["Location"]).data.decode()
        self.assertIn("NEEDS REVIEW", page)
        self.assertNotIn("{&#34;", page)  # no raw JSON

    def test_admin_requires_login(self):
        self.assertEqual(self.client.get("/admin").status_code, 302)
        self.assertEqual(self.client.get("/admin/master").status_code, 302)
        self.assertEqual(self.client.post("/invoices/1/action", data={"action": "approve"}).status_code, 302)

    def test_wrong_password(self):
        self.assertIn(b"Incorrect password", self.client.post("/admin/login", data={"password": "nope"}).data)

    def test_admin_flow(self):
        self.login()
        s = SAMPLES["bank"]
        loc = self.client.post("/submit", data={"submitted_by": "T", "department": s["department"], "invoice_text": s["text"]}).headers["Location"]
        iid = int(loc.rsplit("/", 1)[1])
        self.assertIn(b"Approvals dashboard", self.client.get("/admin").data)
        self.client.post(f"/invoices/{iid}/action", data={"action": "reject", "note": "Vendor unreachable"})
        c = db.connect()
        self.assertEqual(db.get_invoice(c, iid)["status"], "rejected")

    def test_add_and_block_vendor(self):
        self.login()
        self.client.post("/admin/vendors", data={"name": "Test Supplies LLP", "gstin": "", "bank_last4": "1234"})
        c = db.connect()
        v = c.execute("SELECT * FROM vendors WHERE name='Test Supplies LLP'").fetchone()
        self.assertIsNotNone(v)
        self.client.post(f"/admin/vendors/{v['id']}/toggle")
        self.assertEqual(c.execute("SELECT status FROM vendors WHERE id=?", (v["id"],)).fetchone()[0], "blocked")

    def test_bad_gstin_rejected_on_add(self):
        self.login()
        self.client.post("/admin/vendors", data={"name": "Bad GST Co", "gstin": "27AAKFZ9012E1ZX"})
        self.assertIsNone(db.connect().execute("SELECT 1 FROM vendors WHERE name='Bad GST Co'").fetchone())

    def test_404(self):
        self.assertEqual(self.client.get("/invoices/9999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
