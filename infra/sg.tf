# ─────────────────────────────────────────────────────────────────────────────
# SECURITY GROUP — builder
#
# Ingress: SSH (22) + the cis-image-builder web app (8080) from WITHIN the VPC
#          only. Access is via bastion/VPN/console or an internal CLB — never
#          directly from the internet. Ingress CIDR is var.vpc_cidr (NOT
#          hardcoded) so it tracks the IPAM-reserved network automatically.
# Egress:  All outbound (API, yum, pip, git).
# ─────────────────────────────────────────────────────────────────────────────

module "security_groups" {
  source = "../modules/security-group"

  security_groups = {
    "builder" = {
      name        = "${local.name_prefix}-sg-builder"
      description = "CIS image builder — SSH + app from VPC, all outbound"

      ingress = [
        {
          action      = "ACCEPT"
          protocol    = "TCP"
          port        = "22"
          cidr_block  = var.vpc_cidr
          description = "SSH from within VPC"
        },
        {
          action      = "ACCEPT"
          protocol    = "TCP"
          port        = tostring(var.app_port)
          cidr_block  = var.vpc_cidr
          description = "App port — forwarded traffic + health checks from the CLB (in-VPC)"
        },
      ]

      egress = [
        {
          action      = "ACCEPT"
          protocol    = "ALL"
          port        = "ALL"
          cidr_block  = "0.0.0.0/0"
          description = "All outbound — API, yum, pip, git"
        },
      ]
    }

    # Public CLB security group — only your admin IP(s) may reach the CLB.
    # Attaching this SG to the OPEN CLB makes the CLB enforce the restriction.
    "clb" = {
      name        = "${local.name_prefix}-sg-clb"
      description = "Public CLB for CIS platform — restricted to admin IPs"

      ingress = [
        for cidr in var.admin_cidrs : {
          action      = "ACCEPT"
          protocol    = "TCP"
          port        = tostring(var.clb_listen_port)
          cidr_block  = cidr
          description = "CIS platform UI from admin IP"
        }
      ]

      egress = [
        {
          action      = "ACCEPT"
          protocol    = "ALL"
          port        = "ALL"
          cidr_block  = "0.0.0.0/0"
          description = "CLB to backends"
        },
      ]
    }
  }

  tags = local.common_tags
}
