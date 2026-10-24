terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# CAM CUSTOM POLICIES
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_cam_policy" "this" {
  for_each = var.policies

  name        = each.value.name
  description = each.value.description
  document    = jsonencode(each.value.document)
}

# ─────────────────────────────────────────────────────────────────────────────
# CAM ROLES
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_cam_role" "this" {
  for_each = var.roles

  name          = each.value.name
  description   = each.value.description
  console_login = each.value.console_login

  # Trust policy — allows CVM service to assume this role
  document = jsonencode({
    version = "2.0"
    statement = [
      {
        effect = "allow"
        principal = {
          service = each.value.trusted_services
        }
        action = ["name/sts:AssumeRole"]
      }
    ]
  })

  tags = var.tags
}

# ─────────────────────────────────────────────────────────────────────────────
# POLICY ATTACHMENTS
# ─────────────────────────────────────────────────────────────────────────────
locals {
  # Flatten role → built-in policy_ids
  builtin_attachments = {
    for pair in flatten([
      for role_key, role in var.roles : [
        for policy_id in role.policy_ids : {
          key       = "${role_key}|${policy_id}"
          role_key  = role_key
          policy_id = tostring(policy_id)
        }
      ]
    ]) : pair.key => pair
  }

  # Flatten role → custom policy keys
  custom_attachments = {
    for pair in flatten([
      for role_key, role in var.roles : [
        for policy_key in role.custom_policy_keys : {
          key       = "${role_key}|custom|${policy_key}"
          role_key  = role_key
          policy_id = tostring(tencentcloud_cam_policy.this[policy_key].id)
        }
      ]
    ]) : pair.key => pair
  }

  all_attachments = merge(local.builtin_attachments, local.custom_attachments)
}

resource "tencentcloud_cam_role_policy_attachment" "this" {
  for_each = local.all_attachments

  role_id   = tencentcloud_cam_role.this[each.value.role_key].id
  policy_id = each.value.policy_id
}
