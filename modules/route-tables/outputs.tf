output "route_table_ids" {
  description = "Map of route table key → route table ID (named RTs only)"
  value       = { for k, v in tencentcloud_route_table.this : k => v.id }
}

# Full map including "default" — use this when adding routes (NAT GW step)
output "rt_id_map" {
  description = "Map of route table key → route table ID (includes 'default')"
  value       = local.rt_id_map
}
