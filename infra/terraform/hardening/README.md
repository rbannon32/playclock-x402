# TestNet hardening infrastructure

This small Terraform root owns only the infrastructure added after the initial
manual Cloud Run deployment:

- a least-privilege scheduler service account;
- five Cloud Scheduler jobs targeting the existing `ingest` Cloud Run job
  (`ingest-stats` runs `stats,backtest` in one invocation, in that order);
- an email notification channel; and
- three log-matched alerts: the ingest runner's terminal `task failed`
  record, precompute's `board quality flagged` line, and the api's
  `serving the deterministic body` narrator fallback.

It deliberately does not import or manage the existing API, web, ingest, IAM,
Firestore, or domain resources. State is local for this single-operator launch
and is gitignored. If another operator takes over, preserve the state securely
or import these resources before applying.

The two board/narrator alert policies were created by hand through the
Monitoring API on 2026-09-03 and imported into this state the same evening
(`terraform import google_monitoring_alert_policy.board_quality_flagged
projects/playclock/alertPolicies/17550695374573644452`, and likewise
`narrator_degraded` → `1292396670665616484`), then normalised by an apply.
The plan has been clean since; no action is needed before the next apply.

Run from this directory with Application Default Credentials:

```bash
terraform init
terraform plan -var='alert_email=operator@example.com'
terraform apply -var='alert_email=operator@example.com'
```

The schedules use `America/New_York`, invoke the existing job through OAuth,
and pass the same task arguments documented in `infra/deploy.md` §5.
