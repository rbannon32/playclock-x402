variable "project_id" {
  description = "Google Cloud project containing the existing ingest job."
  type        = string
  default     = "playclock"
}

variable "region" {
  description = "Cloud Run and Cloud Scheduler region."
  type        = string
  default     = "us-east4"
}

variable "ingest_job_name" {
  description = "Existing Cloud Run job invoked by the schedules."
  type        = string
  default     = "ingest"
}

variable "api_service_name" {
  description = "Name of the existing api Cloud Run service (for the narrator alert)."
  type        = string
  default     = "api"
}

variable "alert_email" {
  description = "Email address that receives ingest-failure incidents."
  type        = string
}
