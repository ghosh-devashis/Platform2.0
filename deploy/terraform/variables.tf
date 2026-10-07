variable "target" {
  description = "\"local\" (Floci emulator) or \"aws\" (real AWS)."
  type        = string
  default     = "local"
  validation {
    condition     = contains(["local", "aws"], var.target)
    error_message = "target must be \"local\" or \"aws\"."
  }
}

variable "local_endpoint" {
  description = "Floci endpoint, used only when target = \"local\"."
  type        = string
  default     = "http://localhost:4566"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "agent_name" {
  description = "Manifest name (lowercase, hyphens). The runtime name replaces hyphens with underscores."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,47}$", var.agent_name))
    error_message = "agent_name must be 2-48 characters: lowercase letters, digits, hyphens, starting with a letter."
  }
}

variable "team" {
  type = string
}

variable "data_classification" {
  type = string
  validation {
    condition     = contains(["public", "internal", "confidential", "restricted"], var.data_classification)
    error_message = "data_classification must be public, internal, confidential or restricted."
  }
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "description" {
  type    = string
  default = ""
}

variable "image_uri" {
  description = "Container image for the runtime (an ECR URI in real AWS)."
  type        = string
}

variable "secrets" {
  description = "Secrets Manager secret names the agent reads (manifest `secrets`)."
  type        = list(string)
  default     = []
}

variable "s3_bucket_name" {
  description = "Optional S3 bucket for the agent. Empty = none."
  type        = string
  default     = ""
}

variable "dynamodb_table_name" {
  description = "Optional DynamoDB table for the agent (hash key `pk`). Empty = none."
  type        = string
  default     = ""
}

variable "environment_variables" {
  description = "Plain environment variables for the runtime container."
  type        = map(string)
  default     = {}
}
