# ─────────────────────────────────────────────────────────────────────────────
# ssm-secret module — a generic Tencent SSM secret + versioned value.
#
# Reusable wrapper to store any string secret in SSM (tokens, passwords, API
# keys). The value is passed in (often from random_password / random_string so
# it never has to be typed). Retrieve later with:
#   tccli ssm GetSecretValue --SecretName <name> --VersionId <version_id>
#
# NOTE: the secret value lives in Terraform STATE. Keep the COS backend locked
# down. SSM is the access-controlled store for runtime retrieval.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

resource "tencentcloud_ssm_secret" "this" {
  secret_name             = var.secret_name
  description             = var.description
  is_enabled              = true
  recovery_window_in_days = var.recovery_window_in_days
  kms_key_id              = var.kms_key_id != "" ? var.kms_key_id : null
  tags                    = var.tags
}

resource "tencentcloud_ssm_secret_version" "this" {
  secret_name   = tencentcloud_ssm_secret.this.secret_name
  version_id    = var.version_id
  secret_string = var.secret_string
}
