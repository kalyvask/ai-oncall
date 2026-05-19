# Deploy

## 30-minute onboarding

```bash
# 1. Clone + env
git clone https://github.com/kalyvask/ai-oncall.git && cd ai-oncall
cp .env.example .env
# Fill ANTHROPIC_API_KEY if you want real LLM; otherwise mock works.

# 2. Bring up API + Prom + Loki
docker compose up --build -d

# 3. Wait for the API to be ready
until curl -fs http://localhost:8000/ready; do sleep 1; done

# 4. Fire the demo incident
python scripts/demo_incident.py --base http://localhost:8000 --tenant demo
```

The script prints a `report_id` and a URL once the RCA job finishes.

## Slack integration

1. Create a Slack app from the manifest at `deploy/slack-app-manifest.yaml`
   (https://api.slack.com/apps → Create New App → From manifest).
2. Replace the `request_url` lines with your deployment hostname.
3. Install the app to your workspace, then set in `.env`:
   - `AI_ONCALL_SLACK_SIGNING_SECRET` (Basic Information → Signing Secret)
   - `AI_ONCALL_SLACK_BOT_TOKEN` (OAuth & Permissions → Bot User OAuth Token)
   - `AI_ONCALL_SLACK_DEFAULT_CHANNEL` (a channel id, e.g. `C0123456789`).
4. Invite the bot to that channel.

## PagerDuty integration

Wire PagerDuty's HTTP integration to POST to `https://<your-host>/webhooks/alert`
with these transforms in the integration config:

| Source field | Maps to | Notes |
|--------------|---------|-------|
| `incident.id` | `alert_id` | Required, used as idempotency key. |
| `incident.service.name` | `service` | Required. |
| `incident.urgency` | `severity` | `page` for `high`, `warn` for `low`. |
| `incident.created_at` | `fired_at` | ISO-8601. |

Add a custom HTTP header `X-Tenant-Id: <your tenant>` and (if configured)
`Authorization: Bearer <token>` plus `X-Signature: hmac-sha256=<sig>` over
the raw body.

## GitHub App (change correlation)

Create a GitHub App with read access to **Contents** + **Pull requests**
on the repos behind your services. Install on the org, then set:

- `AI_ONCALL_GITHUB_TOKEN` (installation token or PAT)
- `AI_ONCALL_GITHUB_REPO` (e.g. `acme-corp/services`)

The CORRELATE stage will then attach the most recent PR diff for each
hypothesis's `root_cause_service` as evidence.

## Production checklist

- [ ] `AI_ONCALL_WEBHOOK_SIGNING_SECRET` set → unsigned alerts are 401'd
- [ ] `AI_ONCALL_TENANT_TOKENS` set → API requires Bearer auth
- [ ] `AI_ONCALL_SLACK_SIGNING_SECRET` set → Slack endpoints refuse unsigned
- [ ] `AI_ONCALL_CD_DISPATCH_SECRET` set → rollback dispatches are HMAC'd
- [ ] `AI_ONCALL_RAW_SECRETS_BLOCKED=true` if logs may contain credentials
- [ ] Mount `/app/data` on a persistent volume (incidents + jobs DB live here)
- [ ] `/ready` probe wired into your platform's health check
- [ ] `/metrics` scraped by your Prometheus
