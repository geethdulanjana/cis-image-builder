output "route_entry_ids" {
  description = "Map of route key → route entry ID"
  value       = { for k, v in tencentcloud_route_table_entry.this : k => v.id }
}
