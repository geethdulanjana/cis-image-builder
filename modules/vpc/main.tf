terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# VPC
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_vpc" "main" {
  name            = var.vpc_name
  cidr_block      = var.vpc_cidr
  assistant_cidrs = length(var.assistant_cidrs) > 0 ? var.assistant_cidrs : null
  # Disable multicast unless explicitly needed — keeps network clean
  is_multicast = false

  tags = var.tags
}

# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC SUBNETS
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_subnet" "public" {
  for_each = { for s in var.public_subnets : s.name => s }

  vpc_id            = tencentcloud_vpc.main.id
  name              = each.value.name
  cidr_block        = each.value.cidr
  availability_zone = each.value.availability_zone
  is_multicast      = false

  tags = merge(var.tags, { Tier = "public" })
}

# ─────────────────────────────────────────────────────────────────────────────
# PRIVATE SUBNETS
# ─────────────────────────────────────────────────────────────────────────────
resource "tencentcloud_subnet" "private" {
  for_each = { for s in var.private_subnets : s.name => s }

  vpc_id            = tencentcloud_vpc.main.id
  name              = each.value.name
  cidr_block        = each.value.cidr
  availability_zone = each.value.availability_zone
  is_multicast      = false

  tags = merge(var.tags, { Tier = "private" })
}

