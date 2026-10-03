# 3LT Letter Mail — MCP Server

Give your AI assistant hands. **3L-Group Technology** is a print-and-mail rail:
an AI agent calls our MCP tools, and a real physical letter goes out through
the U.S. Postal Service. Draft a letter in chat, approve it, and it's in the
mail — no printing, no envelopes, no post office run.

🌐 **https://3lgrouptechnology.com**

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

## Tools

| Tool | What it does |
|---|---|
| `draft_letter` | Create a letter **draft**: verifies the recipient address, screens content, returns a price quote. **Never mails anything.** Accepts `html` (letter body) or `pdf_path` (server-local PDF). Options: `color`, `quantity` (bulk), `certified` (opt-in USPS Certified Mail + Electronic Return Receipt — standard First-Class is the default). |
| `request_send` | Request that a draft be mailed. Approval-tier keys get a `PENDING` request plus a one-tap Allow/Deny link — nothing is mailed until a human taps Allow. Trusted `on_command` keys may auto-send inside guardrails (spend caps, screening, anomaly detection). |
| `send_status` | Check a send request: status (`PENDING` / `SENT` / `REJECTED` / `FAILED`), approval state, trust-tier decision, and billing state. |

There is **no tool that mails a letter directly**. Every send passes through
an approval record: a human tap or a standing Connect authorization.

## Pricing

Flat **$1.00 service fee per letter**, plus postage (USPS First-Class via our
print-and-mail provider). One price, no tiers, no volume games. Certified Mail
with Electronic Return Receipt is opt-in and costs more — it's never the default.

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

- **MCP server** (`mcp_server.py`) — the 3 tools above over streamable HTTP.
- **REST API** (`api.py`, FastAPI) — agent endpoints (`X-API-Key`) for drafts
  and send requests; admin endpoints (`X-Admin-Token`) to issue keys,
  approve/reject sends, manage trust tiers + spend caps; customer dashboard
  (`/customer`), approval dashboard (`/`), audit history, Stripe webhooks.
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
- **Billing** (Stripe, test mode only) — SetupIntents for saving payment
  methods, off-session **authorize → fulfill → capture** on every approval
  (authorization voided if the send fails), idempotency keys per send.
- **Risk engine** — spending limits are risk-derived, not static. New keys
  start at L0 ($50/mo); clean history auto-raises limits; flags, failed
  payments, and chargebacks tighten or freeze the key. Every decision
  audited with human-readable reasons. See `risk.py`.
- **Abuse screening** — content blocklist runs at draft time AND again at
  approval/fulfillment time.
- **Storage** — SQLite (`db.py`): api_keys, drafts, send_requests,
  billing_receipts, approval_tokens, audit_log.

### Environment variables

| Var | Required | Default | Purpose |
|---|---|---|---|
| `POSTGRID_API_KEY` | yes, to mail | — | PostGrid Print & Mail API key. `test_...` = simulated sends, nothing mailed. Live key = real mail (refused unless `ALLOW_LIVE_MAIL=1`). **Never commit this.** |
| `LMS_ADMIN_TOKEN` | recommended | random, printed once at startup | Admin token for approvals + key issuance (`X-Admin-Token`). |
| `LMS_DB_PATH` | no | `./lms.db` | SQLite file location. |
| `POSTGRID_BASE_COST_USD` | no | `0.97` | Estimated PostGrid per-letter base used in quotes. |
| `POSTGRID_AV_API_KEY` | no | — | PostGrid Address Verification key; falls back to `POSTGRID_API_KEY`. |
| `MAIL_PROVIDER` | no | `postgrid` | `postgrid` or `lob` (legacy fallback). |
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
```

### Deploy notes

- One small VPS is plenty. Run the API behind Caddy/Nginx with TLS.
  Keep the MCP port on localhost or behind the same TLS proxy.
- Process manager: a systemd unit per process — `uvicorn api:app` on :8000
  and `python mcp_server.py` on :8001, both with `Restart=always` and env
  vars from an `EnvironmentFile` (never in the unit file itself).
- SQLite is fine to start; move to Postgres with concurrent writers.
- Back up `lms.db` — it holds your audit trail.

### Safety notes

- There is **no code path** that calls `mail_provider.create_letter` except
  `core.approve_send`, which requires a `PENDING` request and a human admin
  action. The MCP tools cannot send.
- `postgrid_client` sets `trust_env=False` so the API key is never routed
  through ambient proxy env vars.
- API keys are stored as SHA-256 hashes; the raw key is shown once at issue.
