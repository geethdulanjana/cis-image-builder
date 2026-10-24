terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_route_table" "this" {
  for_each = var.route_tables

  vpc_id = var.vpc_id # passed in — this module has no VPC resource
  name   = each.value.name
  tags   = merge(var.tags, each.value.tags)
}

# ─────────────────────────────────────────────────────────────────────────────
# LOCAL: full RT key → ID map (named RTs + VPC default RT)
# ─────────────────────────────────────────────────────────────────────────────
locals {
  rt_id_map = merge(
    { "default" = var.default_route_table_id },
    { for k, v in tencentcloud_route_table.this : k => v.id }
  )

  # Flatten all subnets (public + private) into one map for association
  all_subnets = merge(var.public_subnets, var.private_subnets)
}

# ─────────────────────────────────────────────────────────────────────────────
# SUBNET → ROUTE TABLE ASSOCIATIONS
# Tencent Cloud uses tencentcloud_route_table_association to explicitly
# bind a subnet to a route table (same concept as AWS RT association).
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_route_table_association" "this" {
  for_each = local.all_subnets

  subnet_id      = each.value.id
  route_table_id = local.rt_id_map[each.value.route_table_key]
}
