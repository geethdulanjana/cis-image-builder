# ─────────────────────────────────────────────────────────────────────────────
# ssh-keypair module — outputs
# ─────────────────────────────────────────────────────────────────────────────

output "key_id" {
  description = "Tencent CVM key pair id (skey-xxxx) to attach to instances."
  value       = tencentcloud_key_pair.this.id
}

output "key_name" {
  description = "Name of the Tencent CVM key pair."
  value       = tencentcloud_key_pair.this.key_name
}

output "public_key_openssh" {
  description = "The OpenSSH-format public key that was registered."
  value       = trimspace(tls_private_key.this.public_key_openssh)
}

output "ssm_secret_name" {
  description = "Name of the SSM secret holding the PEM private key."
  value       = tencentcloud_ssm_secret.this.secret_name
}

output "ssm_secret_version_id" {
  description = "Version id of the SSM secret value (where the key is stored)."
  value       = tencentcloud_ssm_secret_version.this.version_id
}

output "private_key_pem" {
  description = "The PEM private key. Prefer fetching from SSM; this is marked sensitive."
  value       = tls_private_key.this.private_key_pem
  sensitive   = true
}
