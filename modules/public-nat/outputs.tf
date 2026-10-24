# Map of AZ key → NAT Gateway ID
# e.g. { "1a" = "nat-xxxxxxxx" }
# When future AZs are added, they automatically appear here.
output "nat_gateway_ids" {
  description = "Map of AZ key → public NAT Gateway ID. Use for route table entries."
  value       = { for k, v in tencentcloud_nat_gateway.this : k => v.id }
}

output "nat_gateway_names" {
  description = "Map of AZ key → public NAT Gateway name"
  value       = { for k, v in tencentcloud_nat_gateway.this : k => v.name }
}

output "eip_ids" {
  description = "Map of AZ key → EIP resource ID"
  value       = { for k, v in tencentcloud_eip.this : k => v.id }
}

# Public IPs — whitelist these in external firewalls/services
output "eip_public_ips" {
  description = "Map of AZ key → public IP. Whitelist these in external firewalls."
  value       = { for k, v in tencentcloud_eip.this : k => v.public_ip }
}
