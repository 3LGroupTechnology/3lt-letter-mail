# DEPLOY.md — 3L-Group Technology · AI Letter Mail · Go-Live Runbook

The deployment package is ready. The mail provider is **PostGrid**
Print & Mail (Lob remains as legacy fallback code only). This is the
exact sequence to go live.

## 0. One-time server prep

- One small VPS (1 vCPU / 1 GB is plenty to start).
- Install Docker + the compose plugin.
- Copy this directory to the server (or `git clone` it, once it's in git).
- Put a TLS reverse proxy (Caddy is simplest) in front of port 8000 and
  serve it at `https://mail.yourdomain.com`. Keep port 8001 firewalled —
  only your agents should reach the MCP server.

## 1. PostGrid test key first

1. Create the PostGrid account, grab a **test** API key (`test_...`).
   Print & Mail must be in test mode — sends are simulated, nothing
   is physically mailed.
2. On the server:
   ```bash
   cp .env.example .env
   # edit .env:
   #   POSTGRID_API_KEY=test_your_key
   #   LMS_ADMIN_TOKEN=<long random secret>   # generate: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ./deploy.sh
   ```
3. Open the dashboard, issue an agent API key (`POST /v1/keys` with
   `X-Admin-Token`), then run the full loop: draft → request send →
   approve → **simulated** send. Nothing is mailed on a test key.

## 2. Go live

1. Add a payment method in the PostGrid dashboard and switch the
   Print & Mail product out of test mode.
2. Swap `POSTGRID_API_KEY` to the **live** key in `.env` AND set
   `ALLOW_LIVE_MAIL=1` (live keys are refused without it), redeploy:
   `docker compose up -d --build`.
3. Send one real letter to yourself. Approve it in the dashboard, confirm
   it arrives, then you're taking customers.

## 3. Ongoing

- **Backups** — `lms.db` holds keys, drafts, and the audit trail:
  ```bash
  docker run --rm -v lms-data:/data -v "$PWD":/backup alpine \
    tar czf /backup/lms-data-$(date +%F).tar.gz /data
  ```
- **Logs** — `docker compose logs -f`.
- **Updates** — rebuild + `docker compose up -d` (the `lms-data` volume
  survives across rebuilds).

## 4. After first revenue (not blocking launch)

- **Customer billing** — currently stubbed. Plan: Stripe customer per API
  key, invoice `estimated_total` when a send is approved. Never collect
  card details in this service.
- **Spending limits** — per-key daily/weekly caps + PostGrid-balance alerts.
- **Abuse screening** — replace the blocklist in `screen.py` with a real
  classifier before opening signups to strangers.
- **Postgres** — if concurrent writers ever contend on SQLite.

## Safety rules (unchanged in production)

- Nothing mails without a `PENDING` request + human approval. The MCP
  tools cannot send.
- `LMS_ADMIN_TOKEN` and `POSTGRID_API_KEY` live in `.env` only — never in chat,
  logs, or git.
