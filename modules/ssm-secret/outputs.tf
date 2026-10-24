output "secret_name" {
  description = "Name of the SSM secret."
  value       = tencentcloud_ssm_secret.this.secret_name
}

output "version_id" {
  description = "Version id of the stored value."
  value       = tencentcloud_ssm_secret_version.this.version_id
}
