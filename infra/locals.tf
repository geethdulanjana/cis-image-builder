# ─────────────────────────────────────────────────────────────────────────────
# LOCALS — naming + standard org tags (mirrors the dev/cis-pipeline pattern).
# ─────────────────────────────────────────────────────────────────────────────

locals {
  name_prefix = "${var.project}-${var.environment}"

  common_tags = {
    "dialog:Environment"        = var.environment
    "dialog:Project"            = var.project
    "dialog:Department"         = "IT"
    "dialog:CostCenter"         = "Cloudops"
    "dialog:DataClassification" = "Cloudops"
    "dialog:Owner"              = "Geeth.Dulanjana"
    "dialog:ApplicationName"    = var.project
    "dialog:Budgetcode"         = "Cloudops"
    "dialog:CxoName"            = "IT"
    "ManagedBy"                 = "terraform"
    "LastAppliedBy"             = data.tencentcloud_sts_caller_identity.current.user_id
  }

  # Stable subnet name per key. Derive a clean suffix from the AZ (last segment
  # of e.g. "ap-singapore-1" -> "1") so names read "cis-platform-shared-subnet-1".
  subnet_names = {
    for k, s in var.subnets :
    k => "${local.name_prefix}-subnet-${element(split("-", s.availability_zone), length(split("-", s.availability_zone)) - 1)}"
  }

  # The subnet the builder CVM lands in. Must reference an existing key.
  builder_subnet_name = local.subnet_names[var.builder_az_key]
}

# Fail fast with a clear message if builder_az_key doesn't match a subnet key,
# instead of a cryptic "Invalid index" on local.subnet_names.
check "builder_az_key_valid" {
  assert {
    condition     = contains(keys(var.subnets), var.builder_az_key)
    error_message = "builder_az_key (\"${var.builder_az_key}\") must be one of the subnets keys: ${join(", ", keys(var.subnets))}."
  }
}
