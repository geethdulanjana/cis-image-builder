# ─────────────────────────────────────────────────────────────────────────────
# VARIABLES
#
# CIDRs are intentionally OPEN (no defaults) so they must be supplied from your
# IPAM reservation in terraform.tfvars. This prevents another hardcoded 10.x
# network slipping in.
# ─────────────────────────────────────────────────────────────────────────────

variable "region" {
  description = "Tencent Cloud region where resources are created."
  type        = string
  default     = "ap-singapore"
}

variable "builder_az_key" {
  description = "Which subnet the builder CVM is placed in. MUST match a key in var.subnets (e.g. \"cis-1\")."
  type        = string
}

variable "environment" {
  description = "Environment name (used in resource naming and tags)."
  type        = string
}

variable "project" {
  description = "Project name (used in resource naming and tags)."
  type        = string
}

# ─── NETWORK (fill from IPAM) ─────────────────────────────────────────────────
variable "vpc_cidr" {
  description = "Primary CIDR block for the VPC — reserve from IPAM (e.g. 172.16.10.0/24). No default: must be set in terraform.tfvars."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be a valid CIDR block, e.g. 172.16.10.0/24."
  }
}

variable "subnets" {
  description = <<EOT
Subnets to create, one per availability zone. Key is a short AZ label (e.g.
"1", "2", "4") and each value has the full AZ name and its CIDR (within
vpc_cidr, reserved from IPAM). Three subnets across AZ 1/2/4 are provisioned up
front so future CVMs can land in any zone; only the builder's subnet is used
today.
EOT
  type = map(object({
    availability_zone = string
    cidr              = string
  }))

  validation {
    condition     = alltrue([for s in var.subnets : can(cidrnetmask(s.cidr))])
    error_message = "Every subnets[*].cidr must be a valid CIDR block within vpc_cidr."
  }
}

# ─── BUILDER CVM ──────────────────────────────────────────────────────────────
variable "builder_image_id" {
  description = <<EOT
Image ID the builder CVM boots from. Use your CUSTOM BACKUP image so the box
comes up already configured (tools + cis-image-builder app). No bootstrap runs
when a custom image is supplied. Example: img-xxxxxxxx.
EOT
  type        = string
}

variable "builder_instance_type" {
  description = "Instance type for the builder CVM. Packer + Ansible are light; 2 vCPU / 4 GB is plenty."
  type        = string
  default     = "SA5.MEDIUM4"
}

variable "builder_system_disk_type" {
  description = "System disk type for the builder CVM."
  type        = string
  default     = "CLOUD_BSSD"
}

variable "builder_system_disk_size" {
  description = "System disk size (GB) for the builder CVM. Match or exceed the backup image's system disk."
  type        = number
  default     = 50
}

# ─── PACKER PKRVARS (rendered onto the builder via TAT) ───────────────────────
variable "packer_pkrvars_path" {
  description = "Absolute path of the Packer pkrvars file on the builder CVM that TAT renders."
  type        = string
  default     = "/opt/cis-pipeline/packer/variables.auto.pkrvars.hcl"
}

variable "packer_source_image_id" {
  description = "Base image id the Packer build launches from (source_image_id). Keep in sync with CIS_BASE_IMAGE_ID in the app's systemd unit."
  type        = string
  default     = "img-4o7u81vk"
}

variable "packer_instance_type" {
  description = "Instance type Packer uses for the transient build CVM."
  type        = string
  default     = "S5.MEDIUM2"
}

variable "packer_image_name_prefix" {
  description = "Prefix for the custom image name Packer produces."
  type        = string
  default     = "rhel10-cis-hardened"
}

variable "packer_image_description" {
  description = "Description for the custom image Packer produces."
  type        = string
  default     = "RHEL 10 CIS Benchmark v2.0.0 hardened image for TKE nodes"
}

variable "app_port" {
  description = "Port the cis-image-builder app listens on inside the CVM (matches the systemd unit; uvicorn --port). The public CLB forwards to this port."
  type        = number
  default     = 8080
}

# ─── APP DEPLOY (Ansible via TAT) ─────────────────────────────────────────────
variable "app_repo_url" {
  description = "Git URL of the cis-image-builder repo the builder deploys from."
  type        = string
  default     = "https://github.com/geethdulanjana/cis-image-builder.git"
}

variable "deploy_ref" {
  description = "Git ref (branch/tag/commit) to deploy. Change this (or deploy_run_id) to trigger a redeploy."
  type        = string
  default     = "main"
}

variable "deploy_run_id" {
  description = <<EOT
Bump this (any new value) to force a redeploy without changing the ref — e.g.
to re-pull the same branch after a push. On-demand trigger for the deploy.
EOT
  type        = string
  default     = "1"
}

variable "app_dir" {
  description = "Directory on the builder where the app repo is checked out."
  type        = string
  default     = "/root/cis-image-builder"
}

variable "token_version" {
  description = <<EOT
Version label for the app auth token, e.g. "v1". BUMP THIS to rotate: a new
random token is generated, stored as a new SSM version, and the next deploy
picks it up. The SSM secret name's VersionId tracks this value.
EOT
  type        = string
  default     = "v1"
}

variable "clb_listen_port" {
  description = "Public port the CLB listens on."
  type        = number
  default     = 80
}

variable "admin_cidrs" {
  description = <<EOT
Public source CIDRs allowed to reach the CLB (your office/home public IP).
Use /32 for a single IP, e.g. ["203.0.113.45/32"]. No default — must be set so
the platform is never exposed to 0.0.0.0/0.
EOT
  type        = list(string)

  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for c in var.admin_cidrs : can(cidrnetmask(c))])
    error_message = "admin_cidrs must be a non-empty list of valid CIDRs (e.g. [\"203.0.113.45/32\"])."
  }
}

variable "run_bootstrap" {
  description = <<EOT
Whether to run the first-boot bootstrap script on the builder CVM.
Set FALSE (default) when booting from a custom backup image that is already
provisioned. Set TRUE only if booting from a bare marketplace RHEL image.
EOT
  type        = bool
  default     = false
}
