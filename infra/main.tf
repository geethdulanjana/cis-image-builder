# ─────────────────────────────────────────────────────────────────────────────
# CIS Image Builder — Infrastructure
#
# Purpose: Minimal, isolated VPC + a single builder CVM that runs the
#          cis-image-builder application (FastAPI + Packer pipeline) which
#          produces CIS-hardened RHEL images for TKE worker nodes.
#
# This is a FRESH environment that replaces the old out-of-policy 10.x build
# infra. The VPC/subnet CIDRs are open variables — fill them from your IPAM
# reservation in terraform.tfvars. The builder CVM boots from a custom backup
# image (no bootstrap needed), so it comes up already configured.
#
# State: remote in the shared COS backend (survives laptop/CVM loss).
# Modules: live alongside this config at ../modules (single source of truth).
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.5"

  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }

  # Remote state in the shared COS bucket. A distinct prefix keeps this env's
  # state separate from the old cis-pipeline env (do NOT reuse that prefix).
  backend "cos" {
    region = "ap-singapore"
    bucket = "tf-state-nprod-1452370164"
    prefix = "cis-image-builder"
  }
}

provider "tencentcloud" {
  region = var.region
}

# Account id (for CAM policy resource ARNs) and AZ validation.
data "tencentcloud_sts_caller_identity" "current" {}
data "tencentcloud_availability_zones" "default" {}
