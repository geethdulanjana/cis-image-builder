# ─────────────────────────────────────────────────────────────────────────────
# CLB — PUBLIC load balancer fronting the CIS platform UI.
#
# OPEN (public) CLB with a public VIP. A security group restricts ingress to
# var.admin_cidrs (your public IP) so the platform is not open to the world.
# Listener forwards the public port (80) to the app port (8080) on the builder
# CVM, health checked over HTTP against the app's status path.
# ─────────────────────────────────────────────────────────────────────────────

module "clb_builder" {
  source = "../modules/clb"

  clb_name     = "${local.name_prefix}-clb-builder"
  network_type = "OPEN"
  vpc_id       = module.vpc.vpc_id
  project_id   = 0

  # Restrict who can reach the public CLB to admin IP(s).
  security_groups = [module.security_groups.security_group_ids["clb"]]

  # Bypass backend SG: the CLB preserves the client's real source IP, so the
  # backend CVM's SG would otherwise block it. Access control stays on the CLB
  # SG above (admin IPs only). Matches the console toggle you enabled.
  pass_to_target = true

  listeners = {
    "app" = {
      port                   = var.clb_listen_port # public: 80
      protocol               = "TCP"
      scheduler              = "WRR"
      health_check_switch    = true
      health_check_type      = "HTTP"
      health_check_http_path = "/"
      targets = [
        {
          instance_id = module.cvm.instance_ids["cis-image-builder-1a"]
          port        = var.app_port # backend: 8080
          weight      = 10
        },
      ]
    }
  }

  tags = local.common_tags
}
