terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

resource "tencentcloud_vpc_private_nat_gateway" "this" {
  for_each         = var.nat_gateways
  cross_domain     = true
  nat_gateway_name = each.value.name
  # vpc_id           = var.vpc_id
  ccn_id = each.value.ccn_id

}
