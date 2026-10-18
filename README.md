# InvoiceLens

Finance · **Invoice Decision Tool**. Paste a supplier invoice; InvoiceLens extracts the details, checks them against
vendors, purchase orders and budgets, and recommends **Approve / Needs review / Reject** with the reasons and the right approver.

```
Invoice text → form validation → mask account numbers → extraction (AI + rules, grounded)
            → logic layer (23 risk rules, score, decision, approver) → SQLite → decision report → admin dashboard
```

## Files
| File | Job |
|---|---|
| `app.py` | Flask routes, admin login, template filters |
| `validation.py` | Form checks |
| `extraction.py` | Masking, rule-based extraction, AI extraction, grounding, confidence |
| `llm_client.py` | OpenAI-compatible client (Gemini default) |
| `risk_engine.py` | The logic layer: rule catalogue, scoring, decision, approver tiers |
| `db.py` | Schema, master-data seed, lookup used by the rules, dashboard queries |
| `pipeline.py` | Runs the whole flow, stores the result, human approve / reject / override |
| `textutil.py` | Name matching, GSTIN checksum, rupee formatting |

## Logic layer
Each flag adds points (critical 50, high 25, medium 15, low 5; capped at 100). Review from 15, reject from 70.
Some flags force an outcome (duplicate invoice or blocked vendor → reject; unreadable invoice or changed bank account → review).
Approver by amount: ≤ ₹25,000 Finance Executive, ≤ ₹1,00,000 Finance Manager, ≤ ₹5,00,000 Finance Head, above that CFO.
Clean invoices up to ₹25,000 are approved automatically.

## AI guardrails
Account numbers are masked to the last 4 digits before storage or any AI call. Every value the AI returns must literally appear in the
invoice text or it is discarded. If the AI fails or no key is set, rule-based extraction takes over and the page says so.

## Run on Replit
Add Secrets: `ADMIN_PASSWORD`, `SECRET_KEY`, `LLM_API_KEY` (Gemini key, optional), optionally `LLM_MODEL=gemini-flash-latest`. Press Run.
On the Admin dashboard, press **Add demo history**, then try the samples on the home page.

## Tests
`python -m unittest discover -s tests -t .` (32 tests)
