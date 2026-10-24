# CIS Image Builder — Infrastructure

Fresh, isolated VPC + a single builder CVM that runs the `cis-image-builder`
app and its Packer pipeline. Replaces the old out-of-policy `10.10.0.0/24` build
network (`infra-rnd/tc-vpc/envs/cis-pipeline`).

## What this creates

- **VPC** with a single private subnet — CIDRs come from `terraform.tfvars`
  (reserved from IPAM, no hardcoded 10.x).
- **Public NAT gateway** + default route for egress (packer/API, yum, pip, git).
- **Security group** `builder` — SSH (22) and the app (8000) ingress from
  *within the VPC only*; all egress. Ingress CIDR tracks `var.vpc_cidr`.
- **CAM role** `builder` — lets Packer (running on the CVM) create/destroy temp
  build CVMs and snapshot custom images via instance-metadata credentials.
- **Builder CVM** booted from your **custom backup image**, private IP only.

Modules live alongside this config at `../modules` (moved into the project so
the whole thing is self-contained).
Remote state: COS bucket `tf-state-nprod-1452370164`, prefix `cis-image-builder`
(distinct from the old `cis-pipeline` prefix).

## Before you apply

1. **Reserve a CIDR from IPAM** and set in `terraform.tfvars`:
   - `vpc_cidr`    (e.g. `172.16.10.0/24`)
   - `subnet_cidr` (within the VPC CIDR, e.g. `172.16.10.0/26`)
2. **Set `builder_image_id`** to your custom backup image (`img-xxxxxxxx`).
   Keep `run_bootstrap = false` so cloud-init does not re-provision a restored
   image. (Only set it `true` if you point at a bare marketplace RHEL image.)
3. Confirm `ssh_key_id` is a valid existing key pair.

## Deploy

```bash
cd cis-image-builder/infra
terraform init        # configures the COS backend (cis-image-builder prefix)
terraform plan -out tfout
terraform apply tfout
```

Outputs include `builder_private_ip`, `builder_subnet_id`, `vpc_id`, and
`security_group_id`.

## After apply — point Packer at the new network

The Packer build (`rhel-cis-official/packer/variables.auto.pkrvars.hcl`) targets
its own `vpc_id` / `subnet_id` / `security_group_id`. Update those three to the
new ids from the outputs so builds launch their temp CVMs in this VPC:

```hcl
vpc_id            = "<vpc_id output>"
subnet_id         = "<builder_subnet_id output>"
security_group_id = "<security_group_id output>"
```

## Decommissioning the OLD environment

This is a separate Terraform state, so the old `cis-pipeline` env is untouched.
Destroy it on its own once this one is verified:

```bash
cd ../../infra-rnd/tc-vpc/envs/cis-pipeline
terraform destroy
```

Custom images you already built live in the Tencent image registry, not on the
old CVM, so they survive the destroy.

## Notes

- No public IP is assigned. Reach the app via bastion/VPN/console, or front
  port 8000 with an internal CLB.
- `assistant_cidrs` is available on the VPC module if you ever need a secondary
  range, but the primary CIDR is the only one routable over VPC peering.
