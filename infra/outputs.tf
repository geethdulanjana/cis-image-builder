# ─────────────────────────────────────────────────────────────────────────────
# OUTPUTS — printed after `terraform apply`.
# ─────────────────────────────────────────────────────────────────────────────

output "vpc_id" {
  description = "VPC ID of the CIS image builder network."
  value       = module.vpc.vpc_id
}

output "vpc_cidr" {
  description = "VPC CIDR in use (from IPAM)."
  value       = var.vpc_cidr
}

output "builder_subnet_id" {
  description = "Subnet ID where the builder CVM lives (use for Packer's subnet_id)."
  value       = module.vpc.private_subnet_ids[local.builder_subnet_name]
}

output "subnet_ids" {
  description = "Map of AZ key (1/2/3) -> subnet ID. Use these to place future CVMs in any zone."
  value       = { for k, name in local.subnet_names : k => module.vpc.private_subnet_ids[name] }
}

output "builder_instance_id" {
  description = "Instance ID of the CIS image builder CVM."
  value       = module.cvm.instance_ids["cis-image-builder-1a"]
}

output "builder_private_ip" {
  description = "Private IP of the builder CVM (SSH from within the VPC; app on :8000)."
  value       = module.cvm.private_ips["cis-image-builder-1a"]
}

output "builder_cam_role" {
  description = "CAM role attached to the builder (provides API creds via metadata)."
  value       = module.cam.role_names["builder"]
}

output "security_group_id" {
  description = "Security group ID attached to the builder."
  value       = module.security_groups.security_group_ids["builder"]
}

output "ssh_key_id" {
  description = "Generated Tencent CVM key pair id (skey-xxxx) attached to the builder."
  value       = module.ssh_keypair_bastion.key_id
}

output "ssh_private_key_secret" {
  description = "SSM secret name holding the PEM private key. Fetch: tccli ssm GetSecretValue --SecretName <name> --VersionId v1"
  value       = module.ssh_keypair_bastion.ssm_secret_name
}

output "nat_gateway_id" {
  description = "NAT gateway ID providing internet egress."
  value       = module.public_nat.nat_gateway_ids["1a"]
}

output "clb_id" {
  description = "Internal CLB id fronting the builder app."
  value       = module.clb_builder.clb_id
}

output "app_url" {
  description = "Public URL of the CIS platform via the CLB (reachable only from admin_cidrs)."
  value       = "http://${try(module.clb_builder.clb_vip[0], "<clb-public-vip>")}:${var.clb_listen_port}"
}

output "next_steps" {
  description = "What to do after terraform apply completes."
  value       = <<-EOT

    ============================================================
      CIS Image Builder infrastructure deployed.
    ============================================================
      VPC CIDR     : ${var.vpc_cidr}
      Subnets      : ${join(", ", [for k, s in var.subnets : "${s.availability_zone}=${s.cidr}"])}
      Builder IP   : (see builder_private_ip output)

      Packer still targets its OWN vpc/subnet/sg in
      rhel-cis-official/packer/variables.auto.pkrvars.hcl —
      update those to the new ids:
        vpc_id, subnet_id, security_group_id  (from outputs above)

      Reach the app: SSH via bastion/VPN, or front :8000 with an
      internal CLB. No public IP is assigned.
    ============================================================
  EOT
}
