# InvoiceLens: architecture notes

**Problem.** Accounts payable staff read each supplier invoice by hand and check it against vendors, POs and budgets. Mistakes (duplicates, changed bank details, over-billing) become payments that are hard to recover.

**Flow.** Form → validation → mask account numbers → extraction (AI, then rules fill gaps, AI values must be grounded in the text) → logic layer (23 rules, score, decision, approver) → SQLite → decision report → admin dashboard (approve / reject / override).

**Layers.** `app.py` (routes) · `validation.py` · `extraction.py` + `llm_client.py` · `risk_engine.py` (logic layer) · `db.py` (storage + lookups) · `pipeline.py` (orchestration, human decisions).

**Data.** vendors, purchase_orders, departments (master data) and invoices (one row per checked invoice, with extracted fields, score, decision, status, reviewer note, override flag and AI usage).

**Decision rule.** Points per flag: critical 50, high 25, medium 15, low 5 (cap 100). Reject at 70+ or a forcing flag; review at 15+ or a forcing flag; otherwise approve. Approver from amount tier. Auto-approve up to ₹25,000.
