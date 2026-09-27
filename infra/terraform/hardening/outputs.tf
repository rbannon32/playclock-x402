output "scheduler_service_account" {
  value = google_service_account.scheduler.email
}

output "scheduler_jobs" {
  value = sort(keys(google_cloud_scheduler_job.ingest))
}

output "ingest_failure_policy" {
  value = google_monitoring_alert_policy.ingest_failure.name
}

output "notification_channel" {
  value = google_monitoring_notification_channel.operator_email.name
}
