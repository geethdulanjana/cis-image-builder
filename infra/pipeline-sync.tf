# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE SYNC via TAT — keep /opt/cis-pipeline current from git, no hand-copy.
#
# The Packer + Ansible CIS pipeline (rhel-cis-official) runs on the builder at
# /opt/cis-pipeline. Instead of copying pipeline changes onto the box, Terraform
# ships a TAT command that git-pulls the repo into that dir. The runtime-rendered
# files (variables.auto.pkrvars.hcl, ansible/group_vars/all.yml) are gitignored
# in the repo, so the hard-reset never clobbers them — the render-pkrvars flow
# and this sync coexist.
#
# Trigger: ON-DEMAND. Change var.pipeline_ref or bump var.pipeline_run_id to
# re-sync (e.g. after pushing a packer/ansible change). The invoke is replaced
# when the rendered script changes.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  sync_pipeline_script = templatefile("${path.module}/templates/sync-pipeline.sh.tftpl", {
    repo_url        = var.pipeline_repo_url
    pipeline_ref    = var.pipeline_ref
    pipeline_run_id = var.pipeline_run_id
    pipeline_dir    = var.pipeline_dir
  })
}

resource "tencentcloud_tat_command" "sync_pipeline" {
  command_name      = "${local.name_prefix}-sync-pipeline"
  description       = "Sync the CIS pipeline (packer + ansible) into /opt/cis-pipeline from git."
  command_type      = "SHELL"
  username          = "root"
  working_directory = "/root"
  timeout           = 600
  content           = local.sync_pipeline_script

  tags {
    key   = "ManagedBy"
    value = "terraform"
  }
  tags {
    key   = "Purpose"
    value = "pipeline-sync"
  }
}

resource "terraform_data" "sync_pipeline_hash" {
  input = sha256(local.sync_pipeline_script)
}

resource "tencentcloud_tat_invocation_invoke_attachment" "sync_pipeline" {
  command_id        = tencentcloud_tat_command.sync_pipeline.id
  instance_id       = module.cvm.instance_ids["cis-image-builder-1a"]
  username          = "root"
  working_directory = "/root"

  lifecycle {
    replace_triggered_by = [terraform_data.sync_pipeline_hash]
  }

  depends_on = [module.cvm]
}
