# Alert source integrations

ai-oncall ingests alerts through one endpoint:

```
POST /webhooks/alert
X-Tenant-Id: <tenant>                          required
Authorization: Bearer <token>                  required when AI_ONCALL_TENANT_TOKENS is set
X-Signature: hmac-sha256=<hex>                 required when AI_ONCALL_WEBHOOK_SIGNING_SECRET is set
Content-Type: application/json
```

The body must validate against `schemas/alert.json`. Required fields:
`alert_id`, `tenant_id` (or it is taken from the header), `fired_at`,
`source` (`pagerduty` / `opsgenie` / `grafana` / `datadog` / `manual` /
`slack`), `severity` (`page` / `warn` / `info`), `service`, `title`, and a
`signal` object with at least `kind` (`metric_threshold` / `log_pattern` /
`trace_anomaly` / `synthetic_probe` / `manual`).

The endpoint returns `202` with a `job_id`; poll `GET /jobs/{job_id}` for
the RCA. Posts are idempotent on `(tenant_id, alert_id)`: a duplicate
returns the existing job.

The signature is HMAC-SHA256 over the raw request body with
`AI_ONCALL_WEBHOOK_SIGNING_SECRET`:

```bash
BODY='{"alert_id":"a-123", ...}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')
curl -X POST https://<host>/webhooks/alert \
  -H "X-Tenant-Id: demo" \
  -H "X-Signature: hmac-sha256=$SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

Most alerting systems cannot compute this HMAC themselves, so the usual
pattern is a thin relay (serverless function or a rule in your existing
webhook router) that reshapes the vendor payload into `schemas/alert.json`
and signs it. The sections below give the vendor-side config and the
field mapping the relay needs.

## Prometheus Alertmanager

`alertmanager.yml`:

```yaml
receivers:
  - name: ai-oncall
    webhook_configs:
      - url: https://<relay-host>/alertmanager-to-ai-oncall
        send_resolved: false

route:
  receiver: ai-oncall
  group_by: [alertname, service]
```

Relay mapping from the Alertmanager webhook payload (`alerts[0]`):

| alert.json field | Alertmanager source |
|---|---|
| `alert_id` | `fingerprint` |
| `fired_at` | `startsAt` |
| `source` | `"manual"` (or `"grafana"` if routed via Grafana Alerting) |
| `severity` | `labels.severity` mapped to `page` / `warn` / `info` |
| `service` | `labels.service` (set via `group_by` or a relabel rule) |
| `title` | `annotations.summary` |
| `signal.kind` | `"metric_threshold"` |
| `labels` | `labels` (pass through) |

## Grafana Alerting

Create a webhook contact point (Alerting > Contact points > New >
Webhook) with the relay URL. Grafana's payload carries `alerts[].labels`
and `alerts[].annotations` in the same shape as Alertmanager, so the
mapping above applies; set `source` to `"grafana"`. Use the
`ai-oncall` annotations to carry `service` when your labels do not
already have it.

## PagerDuty

Add a webhook subscription (Integrations > Generic Webhooks > v3) scoped
to the service, listening to `incident.triggered`. Relay mapping from the
v3 payload (`event.data`):

| alert.json field | PagerDuty source |
|---|---|
| `alert_id` | `id` |
| `fired_at` | `created_at` |
| `source` | `"pagerduty"` |
| `severity` | `priority.summary` mapped to `page` / `warn` / `info` (default `page`) |
| `service` | `service.summary` |
| `title` | `title` |
| `signal.kind` | `"manual"` |

## Manual / curl

For testing or human-initiated investigations:

```bash
curl -X POST http://localhost:8000/webhooks/alert \
  -H "X-Tenant-Id: demo" -H "Content-Type: application/json" \
  -d @fixtures/synthetic_alerts/checkout_regression.json
```

Or run `python scripts/demo_incident.py --base http://localhost:8000`
which posts the same fixture and polls the job to completion
(`--signing-secret` and `--token` flags cover signed/authenticated
deployments).

## Slack

Slack is a delivery surface rather than an alert source: the RCA is
posted as Block Kit, `propose`-tier actions render an approval button,
and thread replies trigger bounded follow-up investigations. Set up the
app from `deploy/slack-app-manifest.yaml`; endpoints and required env
vars are documented in [OPERATIONS.md](OPERATIONS.md#slack-surfaces).
