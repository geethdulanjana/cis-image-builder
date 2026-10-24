output "role_ids" {
  description = "Map of role key → role ID"
  value       = { for k, v in tencentcloud_cam_role.this : k => v.id }
}

output "role_names" {
  description = "Map of role key → role name"
  value       = { for k, v in tencentcloud_cam_role.this : k => v.name }
}

output "policy_ids" {
  description = "Map of custom policy key → policy ID"
  value       = { for k, v in tencentcloud_cam_policy.this : k => v.id }
}
