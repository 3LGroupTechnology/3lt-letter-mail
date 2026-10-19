# 3LT Letter Mail — MCP Server

Give your AI assistant hands. **3L-Group Technology** is a print-and-mail rail:
an AI agent calls our MCP tools, and a real physical letter goes out through
the U.S. Postal Service — or a real fax lands on a real fax machine. Draft in
chat, approve it, and it's sent — no printing, no envelopes, no post office run,
no fax machine of your own.

🌐 **https://3lgrouptechnology.com**
📇 Official MCP Registry: `com.3lgrouptechnology/3lt-letter-mail` (see `server.json`)

## Use the hosted service (no setup)

The public MCP endpoint is live:

```
https://3lgrouptechnology.com/mcp
```

Transport: **streamable HTTP**. Authentication: pass your API key as the
`api_key` argument on every tool call. [Get an API key at 3lgrouptechnology.com](https://3lgrouptechnology.com).

### Connect from Claude

Claude (claude.ai) supports custom MCP connectors: add a new connector with
the server URL above. When the assistant calls a tool, it supplies your
`lms_...` API key as the `api_key` parameter. (The service never sees your
Claude account — the key is the only credential.)

### Connect from Cursor

Settings → MCP → Add custom MCP server:

```json
{
  "mcpServers": {
    "3lt-letter-mail": {
      "url": "https://3lgrouptechnology.com/mcp"
    }
  }
}
```

### Connect from any MCP client

Any client that speaks streamable HTTP works: point it at
`https://3lgrouptechnology.com/mcp` and provide your API key per tool call.

### Get an API key

Keys (`lms_...`) are issued by the service — the key is shown exactly once,
so store it when it appears. Two paths:

- **Self-serve (Connect)** — open https://3lgrouptechnology.com/connect (or
  have your assistant `POST /v1/connect`). You save a payment method on the
  Stripe-hosted element (the `/billing` page machinery), confirm a monthly
  cap, and finalize: the service issues an `on_command` key bound to that
  cap. That one authorization is the standing approval — sends inside the
  caps need no per-send prompt.
- **Admin-issued** — the operator creates a key with `POST /v1/keys`
  (`X-Admin-Token`). These start on the `approval` tier: every send needs a
  human Allow tap until the operator changes the tier.

Either way the key starts at risk level L0 ($50/mo cap); clean history
raises limits automatically, and hitting the monthly cap is a hard stop,
never a silent overage.

## Tools

| Tool | What it does |
|---|---|
| `draft_letter` | Create a letter **draft**: verifies the recipient address, screens content, returns a price quote. **Never mails anything.** Accepts `html` (letter body) or `pdf_path` (server-local PDF). Options: `color`, `quantity` (bulk), `certified` (opt-in USPS Certified Mail + Electronic Return Receipt — standard First-Class is the default). |
| `request_send` | Request that a draft be mailed. Approval-tier keys get a `PENDING` request plus a one-tap Allow/Deny link — nothing is mailed until a human taps Allow. Trusted `on_command` keys may auto-send inside guardrails (spend caps, screening, anomaly detection). |
| `send_status` | Check a send request: status (`PENDING` / `SENT` / `REJECTED` / `FAILED`), approval state, trust-tier decision, and billing state. |
| `create_fax_draft` | Create a fax **draft** from a server-local PDF: validates the recipient fax number, counts pages, screens content, returns a price quote. **Never transmits anything.** |
| `approve_fax` | Approve a fax draft so it can be transmitted. Content is re-screened at approval time. Transmits nothing by itself. |
| `send_fax` | Transmit an **approved** fax via Phaxio. Refuses unapproved drafts and already-sent faxes — a fax transmits exactly once. |
| `get_fax_status` | Check a fax: lifecycle status (`DRAFT` / `APPROVED` / `SENT` / `FAILED`), page count, quote, billing state, and live Phaxio delivery status once sent. |

There is **no tool that mails a letter or sends a fax directly**. Every send
passes through an approval record: a human tap or a standing Connect
authorization. Fax is stricter still: `approve_fax` is always required —
there is no zero-prompt fax path, even for `on_command` keys.

## Pricing

Flat **$1.00 service fee per letter**, plus postage (USPS First-Class via our
print-and-mail provider). One price, no tiers, no volume games. Certified Mail
with Electronic Return Receipt is opt-in and costs more — it's never the default.

**Fax:** the same flat shape — Phaxio's per-page cost (**~$0.07/page**) times
the page count, plus the same **$1.00 service fee per fax** (charged once per
fax, not per page). A one-page fax quotes $1.07 before processing gross-up;
the customer's card is charged the grossed-up total on send, exactly like a
letter.

Example: *"Send my 2026 1099s to these 40 people"* → your assistant loops
`draft_letter` → `request_send` → `send_status` for each recipient. Bulk runs
auto-send inside your monthly cap; anything flagged falls back to a one-tap
Allow/Deny link. One itemized receipt per send.

---

## Self-hosting

Prefer to run your own rail? The full stack is in this repo.

### Quickstart

```bash
./quickstart.sh
```

Creates `.venv`, installs deps, runs the test suites, and starts the REST API
on `http://127.0.0.1:8000` (dashboard at `/`).

With no `POSTGRID_API_KEY` set, drafts fail cleanly at address verification —
nothing can be mailed. Set a **test** key to exercise the full flow with
simulated sends:

```bash
POSTGRID_API_KEY=test_... LMS_ADMIN_TOKEN=pick-a-secret ./quickstart.sh
```

MCP server (separate terminal):

```bash
POSTGRID_API_KEY=test_... .venv/bin/python mcp_server.py
# MCP endpoint: http://127.0.0.1:8001/mcp
```

### What's in the repo

- **MCP server** (`mcp_server.py`) — the 7 tools above over streamable HTTP.
- **REST API** (`api.py`, FastAPI) — agent endpoints (`X-API-Key`) for drafts,
  send requests, and the fax lifecycle (`POST /v1/faxes` →
  `/{id}/approve` → `/{id}/send`, `GET /v1/faxes/{id}`); admin endpoints
  (`X-Admin-Token`) to issue keys, approve/reject sends, manage trust tiers +
  spend caps; customer dashboard (`/customer`), approval dashboard (`/`),
  audit history, Stripe webhooks. Static discovery documents are served at
  `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource`
  (truthful API-key metadata — there is no OAuth deployment), and
  `/.well-known/mcp/server-card.json` (Smithery static server card).
- **Fax rail** (`phaxio_client.py`, Phaxio API v2.1) — fax drafts are
  validated, page-counted, screened, and quoted like letters; sending runs
  the same billing engine as letters (`tlt_billing.settle_fax`): risk caps
  are checked against spend **pooled across letters and faxes**, the card is
  authorized → the fax transmits → the charge captures (voided if
  transmission fails), and an itemized receipt is issued.
- **Trust tiers** (per API key) — `approval` (default for admin-issued keys:
  human tap every send) or `on_command` (trusted: auto-sends inside
  guardrails — content screening on every send, per-send + daily spend caps,
  **hard monthly cap**, >3× 7-day anomaly rule, `needs_review` fallback).
  All decisions audited.
- **One-time rail authorization ("Connect")** — `POST /v1/connect` creates a
  Stripe customer + returns a `/connect/<token>` page (card/Apple Pay/Google
  Pay via Payment Element, monthly cap confirmed). Finalizing issues an
  `on_command` API key with per-send approval OFF — the single Connect
  authorization is the standing approval, so sends flow with zero prompts
  inside the caps. Hitting the monthly cap is a hard **402 "raise your cap"**
  — never a silent overage.
- **Billing** (Stripe) — SetupIntents for saving payment
  methods, off-session **authorize → fulfill → capture** on every approval
  (authorization voided if the send fails), idempotency keys per send.
  Letters and faxes share the one engine (`settle` / `settle_fax` in the
  shared `tlt_billing` module). ⚠️ This build's billing module **refuses
  non-test Stripe keys** — real customer charges need the test-mode guard
  lifted first (see `DEPLOY.md`, open questions).
- **Risk engine** — spending limits are risk-derived, not static. New keys
  start at L0 ($50/mo); clean history auto-raises limits; flags, failed
  payments, and chargebacks tighten or freeze the key. Every decision
  audited with human-readable reasons. See `risk.py`.
- **Abuse screening** — content blocklist runs at draft time AND again at
  approval/fulfillment time.
- **Storage** — SQLite (`db.py`): api_keys, drafts, send_requests, faxes,
  billing_receipts, approval_tokens, connect_sessions, audit_log.

### Environment variables

| Var | Required | Default | Purpose |
|---|---|---|---|
| `POSTGRID_API_KEY` | yes, to mail | — | PostGrid Print & Mail API key. `test_...` = simulated sends, nothing mailed. Live key = real mail (refused unless `ALLOW_LIVE_MAIL=1`). **Never commit this.** |
| `LMS_ADMIN_TOKEN` | recommended | random, printed once at startup | Admin token for approvals + key issuance (`X-Admin-Token`). |
| `LMS_DB_PATH` | no | `./lms.db` | SQLite file location. |
| `POSTGRID_BASE_COST_USD` | no | `0.97` | Estimated PostGrid per-letter base used in quotes. |
| `POSTGRID_AV_API_KEY` | no | — | PostGrid Address Verification key; falls back to `POSTGRID_API_KEY`. |
| `MAIL_PROVIDER` | no | `postgrid` | `postgrid` or `lob` (legacy fallback). |
| `PHAXIO_API_KEY` / `PHAXIO_API_SECRET` | for fax | — | Phaxio API credentials. `test_...` = simulated sends. Live credentials are refused unless `ALLOW_LIVE_FAX=1`. Leave blank to run letter-mail only. **Never commit these.** |
| `ALLOW_LIVE_FAX` | no | unset | Set to `1` to allow live Phaxio credentials (real faxes, real per-page charges). |
| `PHAXIO_PER_PAGE_USD` | no | `0.07` | Estimated Phaxio per-page cost used in fax quotes. |
| `LMS_MCP_HOST` / `LMS_MCP_PORT` | no | `127.0.0.1` / `8001` | MCP server bind. |
| `STRIPE_SECRET_KEY` | for billing | — | Test secret key (`sk_test_...`). Non-test keys are refused. |
| `STRIPE_PUBLISHABLE_KEY` | for `/billing` | — | Publishable key for the Payment Element page. |
| `STRIPE_WEBHOOK_SECRET` | for webhooks | — | Signing secret for `POST /v1/webhooks/stripe`. |
| `APPROVAL_TOKEN_SECRET` | recommended | ephemeral (restart-volatile) | HMAC secret for one-tap Allow/Deny links. |
| `PUBLIC_BASE_URL` | for approval links | request URL | Public base URL embedded in approval links. |
| `TLT_MAILER_BACKEND` | no | `log` | `log` writes receipts to `TLT_RECEIPTS_DIR` (no email sent). |
| `TLT_RECEIPTS_DIR` | no | `./receipts` | Where itemized receipt files are written. |
| `LMS_UPLOADS_DIR` | no | `./uploads` | Where uploaded PDFs are stored (per-draft dirs). |
| `LMS_MAX_PDF_BYTES` | no | `10485760` | Max PDF upload size in bytes. |

Copy `.env.example` to `.env` and fill in real values. **Never commit `.env`.**

### Run the tests

```bash
.venv/bin/python test_pricing.py
.venv/bin/python test_api_flow.py       # PostGrid calls stubbed, no network
.venv/bin/python test_billing_tiers.py  # Stripe mocked, no network
.venv/bin/python test_pdf.py            # PDF upload + flow, no network
.venv/bin/python test_risk.py
.venv/bin/python test_customer_dashboard.py
.venv/bin/python test_certified.py      # certified-mail pricing + flow
.venv/bin/python test_grossup.py        # billing gross-up math
.venv/bin/python test_rail.py           # Connect rail authorization flow
.venv/bin/python test_fax.py            # Phaxio stubbed, no network
.venv/bin/python test_fax_billing.py    # fax settle/caps/pooling, Stripe mocked
.venv/bin/python test_server_card.py    # /.well-known/mcp/server-card.json
```

### Deploy notes

See **`DEPLOY.md`** — the step-by-step deploy-night runbook (Docker Compose
via `./deploy.sh`, the env checklist, the shared-library shipping step, and
the post-deploy verification probes).

### Safety notes

- There is **no code path** that calls `mail_provider.create_letter` except
  `core.approve_send`, which requires a `PENDING` request and a human admin
  action. The MCP tools cannot send.
- The only fax transmission path is `core.send_fax`, which requires the
  fax to be in `APPROVED` state (an explicit `approve_fax` first) and runs
  billing before transmitting — a declined card means the fax never sends.
- `postgrid_client` sets `trust_env=False` so the API key is never routed
  through ambient proxy env vars.
- API keys are stored as SHA-256 hashes; the raw key is shown once at issue.
