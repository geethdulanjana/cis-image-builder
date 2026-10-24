# ─────────────────────────────────────────────────────────────────────────────
# APP DEPLOY via TAT + Ansible — no SSH, no copy-paste.
#
# Instead of hand-copying backend/frontend onto the builder, Terraform ships a
# TAT command that (on the builder) pulls the repo and runs the Ansible
# deploy-platform.yml playbook: git pull -> pip deps -> render systemd unit ->
# restart -> health check. Persistent app state (build history, saved
# selections) is kept outside the repo so redeploys never lose it.
#
# Trigger: ON-DEMAND. Change var.deploy_ref (e.g. to a new tag) or bump
# var.deploy_run_id to redeploy. The invoke is replaced when the rendered script
# changes, which re-runs the playbook on the builder.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  deploy_script = templatefile("${path.module}/templates/deploy-platform.sh.tftpl", {
    repo_url             = var.app_repo_url
    deploy_ref           = var.deploy_ref
    deploy_run_id        = var.deploy_run_id
    app_dir              = var.app_dir
    app_port             = var.app_port
    cis_base_image_id    = var.packer_source_image_id
    tencent_region       = var.region
    token_secret_name    = module.auth_token_secret.secret_name
    token_secret_version = module.auth_token_secret.version_id
  })
}

resource "tencentcloud_tat_command" "deploy_platform" {
  command_name      = "${local.name_prefix}-deploy-platform"
  description       = "Deploy/update the cis-image-builder app via Ansible (git pull + systemd restart)."
  command_type      = "SHELL"
  username          = "root"
  working_directory = "/root"
  timeout           = 900 # pip install + git clone can take a while on first run
  content           = local.deploy_script

  tags {
    key   = "ManagedBy"
    value = "terraform"
  }
  tags {
    key   = "Purpose"
    value = "app-deploy"
  }
}

# Re-run the deploy when the rendered script changes (ref / run_id / token /
# port). This is the on-demand trigger: bump deploy_run_id or change deploy_ref.
resource "terraform_data" "deploy_hash" {
  input = sha256(local.deploy_script)
}

resource "tencentcloud_tat_invocation_invoke_attachment" "deploy_platform" {
  command_id        = tencentcloud_tat_command.deploy_platform.id
  instance_id       = module.cvm.instance_ids["cis-image-builder-1a"]
  username          = "root"
  working_directory = "/root"

  lifecycle {
    replace_triggered_by = [terraform_data.deploy_hash]
  }

  depends_on = [module.cvm]
}
