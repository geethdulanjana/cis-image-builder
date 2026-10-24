# ─────────────────────────────────────────────────────────────────────────────
# ssh-keypair module
#
# Generates an SSH key pair in Terraform (tls provider), registers the PUBLIC
# key with Tencent CVM (producing a skey- id you attach to instances), and
# stores the PEM PRIVATE key in Tencent SSM so nobody has to pre-create a key
# or handle the private key by hand.
#
# Flow:
#   tls_private_key            -> generates the keypair (private stays in state)
#   tencentcloud_key_pair      -> imports the public key, yields skey-xxxx
#   tencentcloud_ssm_secret    -> the secret container
#   tencentcloud_ssm_secret_version -> stores the PEM private key as v1
#
# SECURITY NOTE: the private key material lives in Terraform STATE (that's how
# the tls provider works). Keep your COS state backend locked down. SSM is the
# durable, access-controlled store for retrieving the key later.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

# 1. Generate the key pair locally.
resource "tls_private_key" "this" {
  algorithm   = var.algorithm
  rsa_bits    = var.algorithm == "RSA" ? var.rsa_bits : null
  ecdsa_curve = null
}

# 2. Register the public key with Tencent CVM -> produces the skey- id.
resource "tencentcloud_key_pair" "this" {
  key_name   = var.key_name
  public_key = trimspace(tls_private_key.this.public_key_openssh)
}

# 3. SSM secret container for the private key.
resource "tencentcloud_ssm_secret" "this" {
  secret_name             = var.secret_name
  description             = var.secret_description
  is_enabled              = true
  recovery_window_in_days = var.recovery_window_in_days
  kms_key_id              = var.kms_key_id != "" ? var.kms_key_id : null
  tags                    = var.tags
}

# 4. Store the PEM private key as the first version.
resource "tencentcloud_ssm_secret_version" "this" {
  secret_name   = tencentcloud_ssm_secret.this.secret_name
  version_id    = "v1"
  secret_string = tls_private_key.this.private_key_pem
}
