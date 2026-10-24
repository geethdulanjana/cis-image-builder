terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

resource "tencentcloud_cos_bucket" "this" {
  bucket            = "${var.bucket_name}-${var.app_id}"
  acl               = var.acl
  force_clean       = var.force_clean
  versioning_enable = var.versioning_enabled

  dynamic "lifecycle_rules" {
    for_each = var.lifecycle_rules
    content {
      filter_prefix = lifecycle_rules.value.filter_prefix

      dynamic "expiration" {
        for_each = lifecycle_rules.value.expiration_days != null ? [1] : []
        content {
          days = lifecycle_rules.value.expiration_days
        }
      }

      dynamic "non_current_expiration" {
        for_each = lifecycle_rules.value.noncurrent_version_expiration_days != null ? [1] : []
        content {
          non_current_days = lifecycle_rules.value.noncurrent_version_expiration_days
        }
      }
    }
  }

  dynamic "cors_rules" {
    for_each = var.cors_rules
    content {
      allowed_headers = cors_rules.value.allowed_headers
      allowed_methods = cors_rules.value.allowed_methods
      allowed_origins = cors_rules.value.allowed_origins
      expose_headers  = cors_rules.value.expose_headers
      max_age_seconds = cors_rules.value.max_age_seconds
    }
  }

  tags = var.tags
}
