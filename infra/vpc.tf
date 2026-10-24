# ─────────────────────────────────────────────────────────────────────────────
# VPC — Isolated network for the CIS Image Builder
#
# CIDRs come from variables (IPAM reservation) — NOT hardcoded. Three private
# subnets (one per AZ in var.subnets, e.g. ap-singapore-1/2/3), one public NAT
# for egress, one shared default route. The builder CVM uses the AZ-1 subnet;
# the others are reserved for future CVMs.
#
#   VPC CIDR : var.vpc_cidr   (e.g. 172.16.10.0/24)
#   Subnets  : var.subnets    (map of AZ key -> { availability_zone, cidr })
# ─────────────────────────────────────────────────────────────────────────────

module "vpc" {
  source = "../modules/vpc"

  vpc_name = "${local.name_prefix}-vpc"
  vpc_cidr = var.vpc_cidr

  public_subnets = []

  # Three private subnets (one per AZ in var.subnets). All share the single
  # builder route table so each has NAT egress. Future CVMs can land in any AZ.
  private_subnets = [
    for k, s in var.subnets : {
      name              = local.subnet_names[k]
      cidr              = s.cidr
      availability_zone = s.availability_zone
      route_table_key   = "builder-rt"
    }
  ]

  tags = local.common_tags
}

# ─── NAT GATEWAY — internet egress for the private subnet ──────────────────────
# Needed for: packer plugin/API calls, yum/pip, git. No public ingress.
module "public_nat" {
  source = "../modules/public-nat"

  vpc_id = module.vpc.vpc_id

  nat_gateways = {
    "1a" = {
      name          = "${local.name_prefix}-nat-1a"
      eip_name      = "${local.name_prefix}-nat-eip-1a"
      eip_isp       = "BGP"
      eip_bandwidth = 100
    }
  }

  tags = local.common_tags
}

# ─── ROUTE TABLE + DEFAULT ROUTE (0.0.0.0/0 -> NAT) ────────────────────────────
module "route_tables" {
  source = "../modules/route-tables"

  vpc_id                 = module.vpc.vpc_id
  default_route_table_id = module.vpc.default_route_table_id
  public_subnets         = module.vpc.public_subnet_cidrs
  private_subnets        = module.vpc.private_subnet_cidrs

  route_tables = {
    "builder-rt" = {
      name = "${local.name_prefix}-rt-builder"
      tags = { Role = "builder" }
    }
  }

  tags = local.common_tags
}

module "routes" {
  depends_on = [module.route_tables]
  source     = "../modules/routes"
  rt_id_map  = module.route_tables.rt_id_map

  routes = [
    {
      route_table_key = "builder-rt"
      destination     = "0.0.0.0/0"
      next_type       = "NAT"
      next_hub        = module.public_nat.nat_gateway_ids["1a"]
      description     = "Internet egress via public NAT for package downloads and API calls"
    },
  ]
}
