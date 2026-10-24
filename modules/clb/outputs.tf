# ─────────────────────────────────────────────────────────────────────────────
# clb module — outputs
# ─────────────────────────────────────────────────────────────────────────────

output "clb_id" {
  description = "CLB instance id."
  value       = tencentcloud_clb_instance.this.id
}

output "clb_name" {
  description = "CLB instance name."
  value       = tencentcloud_clb_instance.this.clb_name
}

output "clb_vip" {
  description = "Virtual IP(s) of the CLB. For INTERNAL this is the VPC-private address to reach backends through."
  value       = tencentcloud_clb_instance.this.clb_vips
}

output "listener_ids" {
  description = "Map of listener key -> listener id."
  value       = { for k, l in tencentcloud_clb_listener.this : k => l.listener_id }
}
