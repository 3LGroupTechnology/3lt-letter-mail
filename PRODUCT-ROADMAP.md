# 3LT Product Roadmap

## Certified mail option (requested 2026-09-26)
- Add USPS Certified Mail (with return receipt / tracking) as a send option on letters.
- Use cases: legal notices, demand letters, compliance mailings, anything where proof of delivery matters.
- Pricing: higher PostGrid base cost for certified; our flat $1.00 fee unchanged.
- Surfaces: MCP `draft_letter` param, REST draft field, quote breakdown line, dashboard toggle.
- Implementation (built 2026-10-02): `certified: bool` threaded from the MCP
  `draft_letter` tool and the REST `POST /v1/drafts` (+ `/v1/drafts/pdf`
  form) through `core.create_draft`/`create_draft_pdf` down to PostGrid's
  letter-create call, which sets `mailingClass="certified_return_receipt"`
  (USPS Certified Mail with Electronic Return Receipt — tracked end-to-end,
  signature captured on delivery). Drafts store the flag (`drafts.certified`);
  `approve_send` passes it to the provider at fulfillment time.
- Certified pricing: PostGrid's published certified + electronic return
  receipt per-letter rate (default $9.51, env `POSTGRID_CERTIFIED_COST_USD`,
  labeled an estimate — PostGrid bills actuals) + the unchanged flat $1.00
  service fee, Stripe-grossed exactly like normal letters.
  Worked example: 1 certified letter = $9.51 + $1.00 = $10.51 net -> $11.14 charged.
- Status: BUILT 2026-10-02 (test/sandbox only — no live key, no production mail).
