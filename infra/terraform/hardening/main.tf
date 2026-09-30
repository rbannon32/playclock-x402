locals {
  scheduler_service_account = "playclock-scheduler"
  ingest_run_uri            = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${var.ingest_job_name}:run"

  schedules = {
    "ingest-nightly" = {
      description = "Refresh the Sleeper player dump and id map nightly."
      schedule    = "0 4 * * *"
      args        = ["--task", "nightly"]
    }
    "ingest-stats" = {
      # backtest runs in the same invocation, after stats, on purpose: it
      # scores claims against the lines stats just wrote, and a separate
      # schedule could overlap a still-running stats task and grade a
      # half-written week permanently (PR #32 review). The task also checks
      # the weekly_stats freshness marker before scoring any week.
      description = "Refresh nflverse stats, usage, defense and schedules Tue/Thu/Sat, then score last week's claims."
      schedule    = "0 9 * * 2,4,6"
      args        = ["--task", "stats,backtest"]
    }
    "ingest-trending" = {
      description = "Refresh Sleeper trending adds and drops every 30 minutes."
      schedule    = "*/30 * * * *"
      args        = ["--task", "trending"]
    }
    "ingest-precompute" = {
      description = "Keep the five default paid boards warm ahead of cache expiry."
      schedule    = "20 */2 * * *"
      args        = ["--task", "precompute"]
    }
    "ingest-precompute-refresh" = {
      description = "Force fresh boards after each stats refresh."
      schedule    = "40 9 * * 2,4,6"
      args        = ["--task", "precompute", "--force"]
    }
  }
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = local.scheduler_service_account
  display_name = "Play Clock scheduler"
  description  = "Invokes only the Play Clock ingest Cloud Run job."
}

resource "google_project_iam_custom_role" "scheduler_job_runner" {
  project     = var.project_id
  role_id     = "playclockSchedulerJobRunner"
  title       = "Play Clock scheduler job runner"
  description = "Runs the Play Clock ingest job with task argument overrides."
  permissions = [
    "run.jobs.run",
    "run.jobs.runWithOverrides",
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = var.region
  name     = var.ingest_job_name
  role     = google_project_iam_custom_role.scheduler_job_runner.name
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "ingest" {
  for_each = local.schedules

  project          = var.project_id
  region           = var.region
  name             = each.key
  description      = each.value.description
  schedule         = each.value.schedule
  time_zone        = "America/New_York"
  attempt_deadline = "320s"

  retry_config {
    retry_count          = 1
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
    max_doublings        = 3
  }

  http_target {
    http_method = "POST"
    uri         = local.ingest_run_uri
    body = base64encode(jsonencode({
      overrides = {
        containerOverrides = [{ args = each.value.args }]
      }
    }))
    headers = { "Content-Type" = "application/json" }

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}

resource "google_monitoring_notification_channel" "operator_email" {
  project      = var.project_id
  display_name = "Play Clock operator email"
  description  = "Operational alerts for the Play Clock launch."
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  enabled      = true
  force_delete = false
}

resource "google_monitoring_alert_policy" "board_quality_flagged" {
  project      = var.project_id
  display_name = "Play Clock board failed the value gate"
  combiner     = "OR"
  enabled      = true
  severity     = "WARNING"

  conditions {
    display_name = "precompute logged 'board quality flagged'"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="${var.ingest_job_name}"
        jsonPayload.message:"board quality flagged"
      EOT
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator_email.name]

  alert_strategy {
    auto_close = "86400s"
    notification_rate_limit {
      period = "3600s"
    }
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "Play Clock: a warmed board failed the value gate"
    content   = <<-EOT
      `ingest --task precompute` warmed a board that failed `api/evals/quality.py` or scored under the judge's threshold. It is being served for its whole TTL. Read `quality/{key}` in Firestore for the failures and the critique; DESIGN_NOTES §24 explains the rules.
    EOT
  }
}

resource "google_monitoring_alert_policy" "narrator_degraded" {
  project      = var.project_id
  display_name = "Play Clock narrator serving the deterministic body"
  combiner     = "OR"
  enabled      = true
  severity     = "WARNING"

  conditions {
    display_name = "api logged a narrator fallback"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_revision"
        resource.labels.service_name="${var.api_service_name}"
        textPayload:"serving the deterministic body"
      EOT
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator_email.name]

  alert_strategy {
    auto_close = "86400s"
    notification_rate_limit {
      period = "3600s"
    }
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "Play Clock: narrator fell back to the template"
    content   = <<-EOT
      A paid answer on `ENGINE=narrated` was served as the deterministic body because the model call failed, timed out (`NARRATOR_TIMEOUT_SECONDS`) or produced a body that did not validate. One is fine; a sustained rate means every caller is paying for the template again — check Vertex quota and the api service account's `roles/aiplatform.user`.
    EOT
  }
}

resource "google_monitoring_alert_policy" "ingest_failure" {
  project      = var.project_id
  display_name = "Play Clock ingest task failed"
  combiner     = "OR"
  enabled      = true
  severity     = "ERROR"

  conditions {
    display_name = "Ingest task emitted its terminal failure log"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="${var.ingest_job_name}"
        jsonPayload.message="task failed"
      EOT
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator_email.name]

  alert_strategy {
    auto_close = "604800s"
    notification_rate_limit {
      period = "300s"
    }
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "Play Clock ingest failed"
    content   = <<-EOT
      The `${var.ingest_job_name}` Cloud Run job logged `task failed`. Paid boards may become stale or fall back to slow live generation.

      Inspect the failed execution in Cloud Run, fix the upstream or Vertex failure, rerun the task, then confirm `https://api.playclock.xyz/v1/health` and the response cache.
    EOT
  }
}
