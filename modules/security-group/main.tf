terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# SECURITY GROUPS
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_security_group" "main" {
  for_each = var.security_groups

  name        = each.value.name
  description = each.value.description
  tags        = var.tags
}

resource "tencentcloud_security_group_rule_set" "main" {
  for_each = var.security_groups

  security_group_id = tencentcloud_security_group.main[each.key].id

  dynamic "ingress" {
    for_each = each.value.ingress
    content {
      action      = ingress.value.action
      cidr_block  = ingress.value.cidr_block
      protocol    = ingress.value.protocol
      port        = ingress.value.port
      description = ingress.value.description
    }
  }

  dynamic "egress" {
    for_each = each.value.egress
    content {
      action      = egress.value.action
      cidr_block  = egress.value.cidr_block
      protocol    = egress.value.protocol
      port        = egress.value.port
      description = egress.value.description
    }
  }
}
