output "instance_ids" {
  description = "Map of instance key → instance ID"
  value       = { for k, v in tencentcloud_instance.this : k => v.id }
}

output "private_ips" {
  description = "Map of instance key → private IP"
  value       = { for k, v in tencentcloud_instance.this : k => v.private_ip }
}
