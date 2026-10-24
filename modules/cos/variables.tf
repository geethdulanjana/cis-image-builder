variable "bucket_name" {
  description = "Bucket name prefix — account_id is appended to ensure global uniqueness."
  type        = string
}

variable "app_id" {
  description = "Tencent Cloud App ID appended to bucket name (e.g. 1452370164). Different from account ID — find it in console → Account Info → App ID."
  type        = string
}

variable "acl" {
  description = "Canned ACL: private | public-read | public-read-write | authenticated-read."
  type        = string
  default     = "private"
}

variable "force_clean" {
  description = "If true, all objects are deleted on bucket destroy. Use with caution."
  type        = bool
  default     = false
}

variable "versioning_enabled" {
  description = "Enable object versioning."
  type        = bool
  default     = false
}

variable "lifecycle_rules" {
  description = "List of lifecycle rules."
  type = list(object({
    filter_prefix                      = string
    expiration_days                    = optional(number)
    noncurrent_version_expiration_days = optional(number)
  }))
  default = []
}

variable "cors_rules" {
  description = "List of CORS rules."
  type = list(object({
    allowed_headers = list(string)
    allowed_methods = list(string)
    allowed_origins = list(string)
    expose_headers  = list(string)
    max_age_seconds = number
  }))
  default = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
