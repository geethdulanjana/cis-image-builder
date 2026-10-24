variable "secret_name" {
  description = "SSM secret name (unique per region; letters, numbers, '_' and '-'; first char a letter or number)."
  type        = string
}

variable "secret_string" {
  description = "The secret value (plaintext; SSM stores it encrypted). Mark sensitive at the caller."
  type        = string
  sensitive   = true
}

variable "version_id" {
  description = "Version id for this value. Bump to add a new version (e.g. v1 -> v2) when rotating."
  type        = string
  default     = "v1"
}

variable "description" {
  description = "Human-readable description for the secret."
  type        = string
  default     = "Managed by Terraform."
}

variable "recovery_window_in_days" {
  description = "Scheduled-deletion window on destroy. 0 = delete immediately; 1-30 = retain/recoverable."
  type        = number
  default     = 0
}

variable "kms_key_id" {
  description = "Optional KMS CMK id to encrypt the secret. Empty = SSM default CMK."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the secret."
  type        = map(string)
  default     = {}
}
