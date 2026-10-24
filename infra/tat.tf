# ─────────────────────────────────────────────────────────────────────────────
# TAT — render the Packer pkrvars onto the builder, no SSH.
#
# The Packer pipeline on the builder reads
#   /opt/cis-pipeline/packer/variables.auto.pkrvars.hcl
# which must match the live VPC/subnet/SG. Instead of hand-editing that file
# after an infra change, Terraform renders it from its own outputs and pushes it
# via Tencent Automation Tools (TAT), which runs the agent already installed on
# the builder.
#
# Flow:
#   templatefile(render-pkrvars.sh) with live ids
#     -> tencentcloud_tat_command  (the script, content = the ids)
#     -> null_resource + tccli tat InvokeCommand  (runs it on the builder)
# The invoke re-runs ONLY when the rendered content changes (triggers hash), so
# a new VPC id automatically re-syncs the pkrvars. "vpc does not exist" can no
# longer happen from drift.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # Zone = the builder subnet's AZ.
  builder_zone = var.subnets[var.builder_az_key].availability_zone

  render_pkrvars_script = templatefile("${path.module}/templates/render-pkrvars.sh.tftpl", {
    pkrvars_path      = var.packer_pkrvars_path
    region            = var.region
    zone              = local.builder_zone
    instance_type     = var.packer_instance_type
    source_image_id   = var.packer_source_image_id
    vpc_id            = module.vpc.vpc_id
    subnet_id         = module.vpc.private_subnet_ids[local.builder_subnet_name]
    security_group_id = module.security_groups.security_group_ids["builder"]
    image_name_prefix = var.packer_image_name_prefix
    image_description = var.packer_image_description
    tag_environment   = var.environment
    tag_project       = var.project
    validation_pubkey = module.ssh_keypair_bastion.public_key_openssh
  })
}

resource "tencentcloud_tat_command" "render_pkrvars" {
  command_name      = "${local.name_prefix}-render-pkrvars"
  description       = "Render Packer pkrvars from Terraform outputs (managed; do not edit on the box)."
  command_type      = "SHELL"
  username          = "root"
  working_directory = "/root"
  timeout           = 120
  content           = local.render_pkrvars_script

  tags {
    key   = "ManagedBy"
    value = "terraform"
  }
  tags {
    key   = "Purpose"
    value = "packer-pkrvars-render"
  }
}

# Tracks the rendered pkrvars content. When the content changes (e.g. a new
# VPC/subnet/SG id), this resource's input changes, which forces the invoke
# attachment below to be REPLACED → the command re-runs on the builder.
resource "terraform_data" "pkrvars_hash" {
  input = sha256(local.render_pkrvars_script)
}

# Invoke the command on the builder — native provider resource (no tccli / no
# shell), so it runs the same on Windows or the Linux builder. Re-runs only
# when the rendered content changes (via replace_triggered_by).
resource "tencentcloud_tat_invocation_invoke_attachment" "render_pkrvars" {
  command_id        = tencentcloud_tat_command.render_pkrvars.id
  instance_id       = module.cvm.instance_ids["cis-image-builder-1a"]
  username          = "root"
  working_directory = "/root"

  lifecycle {
    replace_triggered_by = [terraform_data.pkrvars_hash]
  }

  # Render pkrvars AFTER the pipeline is synced, so the latest packer templates
  # are present on the builder before we write the pkrvars into them.
  depends_on = [module.cvm, tencentcloud_tat_invocation_invoke_attachment.sync_pipeline]
}
