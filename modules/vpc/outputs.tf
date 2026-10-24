# ─── VPC ──────────────────────────────────────────────────────────────────────
output "vpc_id" {
  description = "The ID of the VPC"
  value       = tencentcloud_vpc.main.id
}

output "vpc_cidr" {
  description = "The primary CIDR block of the VPC"
  value       = tencentcloud_vpc.main.cidr_block
}

output "vpc_name" {
  description = "The name of the VPC"
  value       = tencentcloud_vpc.main.name
}

# ─── SUBNETS ──────────────────────────────────────────────────────────────────
output "public_subnet_ids" {
  description = "Map of public subnet name → subnet ID"
  value       = { for k, v in tencentcloud_subnet.public : k => v.id }
}

output "public_subnet_cidrs" {
  description = "Map of public subnet name → subnet details"
  value = { for s in var.public_subnets : s.name => {
    id                = tencentcloud_subnet.public[s.name].id
    cidr              = s.cidr
    availability_zone = s.availability_zone
    route_table_key   = s.route_table_key
  } }
}

output "private_subnet_ids" {
  description = "Map of public subnet name → subnet ID"
  value       = { for k, v in tencentcloud_subnet.private : k => v.id }
}

output "private_subnet_cidrs" {
  description = "Map of private subnet name → subnet details"
  value = { for s in var.private_subnets : s.name => {
    id                = tencentcloud_subnet.private[s.name].id
    cidr              = s.cidr
    availability_zone = s.availability_zone
    route_table_key   = s.route_table_key
  } }
}


output "default_route_table_id" {
  value = tencentcloud_vpc.main.default_route_table_id
}
