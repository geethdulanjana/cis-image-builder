
# ─── SECURITY GROUPS ──────────────────────────────────────────────────────────
output "security_group_ids" {
  description = "Map of logical SG key → security group ID"
  value       = { for k, v in tencentcloud_security_group.main : k => v.id }
}
