# Real-incident benchmark (starter)

Five cases sourced from public postmortems. Format is the lightweight
`RealIncidentCase`: an alert summary plus the ground-truth root cause and
class. The eval loader synthesizes a minimal `Alert` from this; the agent
runs against it the same as it would a synthetic case.

This is a starter set. The plan in the README's roadmap is to expand to
50-100 cases from Cloudflare, GitHub, Datadog, Atlassian, Google SRE, and
public postmortem archives, plus to wire in the OpenRCA + RCAEval loaders
(already stubbed at `evals/openrca_loader.py` and `evals/rcaeval_loader.py`).

Each case cites its public source URL so the ground-truth label is
auditable. Numbers like p99 are approximated from the postmortem text;
where the original incident's signal isn't a service-graph latency
problem, the alert is paraphrased to fit the agent's input contract.

| case_id | source | family |
|---------|--------|--------|
| cloudflare_2022_bgp | https://blog.cloudflare.com/cloudflare-outage-on-june-21-2022/ | config_drift |
| datadog_2023_systemd | https://www.datadoghq.com/blog/2023-03-08-multiregion-infrastructure-connectivity-issue/ | config_drift |
| aws_2021_kinesis | https://aws.amazon.com/message/12721/ | saturation |
| github_2018_network | https://github.blog/2018-10-30-oct21-post-incident-analysis/ | config_drift |
| atlassian_2022_deletion | https://www.atlassian.com/engineering/post-incident-review-april-2022-outage | deploy_regression |
