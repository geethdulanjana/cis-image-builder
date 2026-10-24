output "nat_gateway_ids" {
  description = "Map of key → private NAT Gateway ID"
  value       = { for k, v in tencentcloud_vpc_private_nat_gateway.this : k => v.id }
}

output "nat_gateway_names" {
  description = "Map of key → private NAT Gateway name"
  value       = { for k, v in tencentcloud_vpc_private_nat_gateway.this : k => v.nat_gateway_name }
}
