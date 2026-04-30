# checkout — runbook

Owner: checkout team
On-call rotation: rotates weekly, see #checkout-oncall

## What this service does

`checkout` accepts `POST /checkout`, validates the cart, calls `payment` to
authorize, calls `shipping` for rate selection, and returns an order id. It
is the user-facing critical path — every prod outage page from this service
is a customer-impacting incident.

## Dependencies (downstream)

- `payment` — synchronous; checkout cannot complete without it
- `cart` — synchronous; reads cart state
- `currency` — synchronous; FX conversion
- `shipping` — synchronous; rate calculation

## Common failure modes

### 1. payment is failing → checkout p99 latency alarms

Most common pattern. checkout retries on payment failure with exponential
backoff up to 3 attempts before returning a 502, so payment errors show up
as **checkout latency**, not checkout 5xx.

**Diagnose.** Check payment's error rate and recent deploys *first*. If
payment shows fresh errors after a deploy in the last hour, it's almost
always a regression in payment, not checkout.

**Fix.** `git revert <sha> && deploy payment`. Do not roll back checkout —
its code is fine.

### 2. cart-db connection saturation

Symptom: checkout latency + cart latency spike together, both p99 > 2s.
Look for `FATAL: too many connections` in cart-db logs. Bump the pool or
restart the largest cart pod to free connections.

### 3. currency rate provider hiccup

Symptom: checkout latency rises, cart and payment are clean. currency
shows external API timeouts. Failover happens automatically within 60s;
if the alert clears in 1 minute, this was it.

## Roll-forward vs. roll-back

Default to roll-back inside the deploy window (Mon–Thu 9am–5pm PT). Roll
forward only if:

- the suspected regression is on a service you maintain, **and**
- you can identify and patch the bug in under 10 minutes, **and**
- it is a deploy-window day.

Outside the deploy window: roll back, always. Investigate Monday.

## Useful commands

```bash
# Check recent deploys across the checkout subgraph
ai-oncall deploys --service checkout --depth 2 --since 2h

# Force RCA from the CLI (does not require Slack)
ai-oncall rca checkout

# Roll back the last payment deploy
git -C ./services/payment revert HEAD --no-edit && ./scripts/deploy payment
```

## Escalation

Page `payment` on-call only if rollback does not resolve within 10 minutes
or if Stripe status is degraded. Tag `@checkout-lead` in
`#incidents` for any incident lasting more than 30 minutes.
