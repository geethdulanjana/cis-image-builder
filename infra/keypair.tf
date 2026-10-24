# ─────────────────────────────────────────────────────────────────────────────
# SSH KEY PAIR — generated in Terraform, private key stored in Tencent SSM.
#
# Replaces the hand-managed ssh_key_id. Terraform generates the pair, registers
# the public key with CVM (yielding a skey- id used by the builder CVM), and
# writes the PEM private key into SSM. Retrieve it later with:
#   tccli ssm GetSecretValue --SecretName <name> --VersionId v1
# ─────────────────────────────────────────────────────────────────────────────

module "ssh_keypair_bastion" {
  source = "../modules/ssh-keypair"

  # Tencent key pair names are limited to 1-25 chars and allow underscores but
  # not hyphens — keep this short and fixed. The SSM secret name has a much
  # larger limit so it keeps the full descriptive prefix.
  key_name    = "cis_platform_builder" # 20 chars
  secret_name = "${local.name_prefix}-builder-ssh-key"

  algorithm = "RSA"
  rsa_bits  = 4096

  secret_description      = "SSH private key (PEM) for the CIS image builder CVM."
  recovery_window_in_days = 0

  tags = local.common_tags
}
