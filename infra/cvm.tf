# ─────────────────────────────────────────────────────────────────────────────
# CVM — CIS Image Builder instance
#
# A single private CVM that runs the cis-image-builder app + Packer pipeline.
# It boots from var.builder_image_id — set this to your CUSTOM BACKUP image so
# the machine comes up already provisioned (tools + app + prior build state).
#
# Bootstrap (user_data) is attached ONLY when var.run_bootstrap = true (i.e. when
# booting from a bare marketplace RHEL image). For a restore-from-backup launch,
# leave run_bootstrap = false so cloud-init does not re-run provisioning.
#
# ACCESS: private only (no public IP). Reach it via bastion/VPN/console, or an
# internal CLB in front of port 8000.
# ─────────────────────────────────────────────────────────────────────────────

module "cvm" {
  source = "../modules/cvm"

  vpc_id = module.vpc.vpc_id

  instances = {
    "cis-image-builder-1a" = {
      name               = "${local.name_prefix}-builder-1a"
      availability_zone  = var.subnets[var.builder_az_key].availability_zone
      image_id           = var.builder_image_id
      instance_type      = var.builder_instance_type
      subnet_id          = module.vpc.private_subnet_ids[local.builder_subnet_name]
      security_group_ids = [module.security_groups.security_group_ids["builder"]]
      system_disk_type   = var.builder_system_disk_type
      system_disk_size   = var.builder_system_disk_size
      key_ids            = [module.ssh_keypair_bastion.key_id]
      cam_role_name      = module.cam.role_names["builder"]

      # Only run bootstrap on a bare image. Restoring from a custom backup image
      # keeps user_data null so provisioning does not run again.
      user_data = var.run_bootstrap ? base64encode(file("${path.module}/scripts/builder-bootstrap.sh")) : null
    }
  }

  tags = local.common_tags
}
