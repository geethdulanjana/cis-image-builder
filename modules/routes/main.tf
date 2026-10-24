terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

locals {
  # Build a unique key per route for for_each
  route_map = {
    for r in var.routes :
    "${r.route_table_key}|${r.destination}|${r.next_type}" => r
  }
}

resource "tencentcloud_route_table_entry" "this" {
  for_each = local.route_map

  route_table_id         = var.rt_id_map[each.value.route_table_key]
  destination_cidr_block = each.value.destination
  next_type              = each.value.next_type
  next_hub               = each.value.next_hub
  description            = each.value.description
}
