terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# EIPs — one per NAT Gateway entry
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_eip" "this" {
  for_each = var.nat_gateways

  name = each.value.eip_name
  #bandwidth_package_id = "bwp-jtvzuky6" #set when internet_charge_type is BANDWIDTH_PACKAGE.
  #internet_service_provider  = each.value.eip_isp
  internet_max_bandwidth_out = each.value.eip_bandwidth   #usints in mbps
  internet_charge_type       = "TRAFFIC_POSTPAID_BY_HOUR" #Valid values: BANDWIDTH_PACKAGE, BANDWIDTH_POSTPAID_BY_HOUR, BANDWIDTH_PREPAID_BY_MONTH and TRAFFIC_POSTPAID_BY_HOUR.

  type = "EIP" #EIP and AnycastEIP and HighQualityEIP and AntiDDoSEIP and ResidentialEIP. Default is EIP
  #anti_ddos_package_id = "xxxxxxxx" #must set when type is AntiDDoSEIP.

  tags = merge(var.tags, { NatAZ = each.key })
}

# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC NAT GATEWAYS — one per map entry
# Key = AZ shortname e.g. "1a", "2b", "3c"
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_nat_gateway" "this" {
  for_each = var.nat_gateways

  name                = each.value.name
  vpc_id              = var.vpc_id
  assigned_eip_set    = [tencentcloud_eip.this[each.key].public_ip]
  nat_product_version = 2
  tags                = merge(var.tags, { NatAZ = each.key })

  depends_on = [tencentcloud_eip.this]
}
