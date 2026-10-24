# ─────────────────────────────────────────────────────────────────────────────
# CAM — IAM role for the builder CVM
#
# Packer (running ON the builder) calls Tencent Cloud API to launch a temporary
# build CVM, SSH in, run Ansible, snapshot a custom image, then destroy the temp
# CVM. Credentials are fetched from the instance metadata service via this role,
# so no static API keys live on the box.
# ─────────────────────────────────────────────────────────────────────────────

module "cam" {
  source = "../modules/cam"

  policies = {
    "builder-cvm-packer" = {
      name        = "${local.name_prefix}-policy-builder-cvm-packer"
      description = "Allow Packer to create/destroy temporary build CVMs and snapshot images"
      document = {
        version = "2.0"
        statement = [
          {
            effect = "allow"
            action = [
              # CVM instance lifecycle
              "name/cvm:RunInstances",
              "name/cvm:DescribeInstances",
              "name/cvm:StartInstances",
              "name/cvm:StopInstances",
              "name/cvm:TerminateInstances",

              # Image lifecycle (snapshot build disk -> custom image; delete old)
              "name/cvm:CreateImage",
              "name/cvm:DescribeImages",
              "name/cvm:ModifyImageAttribute",
              "name/cvm:DeleteImages",

              # SSH key pairs (temporary key for Packer SSH)
              "name/cvm:DescribeKeyPairs",
              "name/cvm:CreateKeyPair",
              "name/cvm:DeleteKeyPairs",
              "name/cvm:AssociateInstancesKeyPairs",
              "name/cvm:DisassociateInstancesKeyPairs",

              # Security groups + VPC/subnet discovery
              "name/cvm:DescribeSecurityGroups",
              "name/vpc:DescribeVpcs",
              "name/vpc:DescribeSubnets",
              "name/vpc:DescribeSecurityGroups",

              "name/finance:trade"
            ]
            resource = ["*"]
          },
        ]
      }
    }

    "builder-cos-readwrite" = {
      name        = "${local.name_prefix}-policy-builder-cos-readwrite"
      description = "Allow builder to read/write CIS artifacts to COS"
      document = {
        version = "2.0"
        statement = [
          {
            effect   = "allow"
            action   = ["name/cos:*"]
            resource = ["qcs::cos:ap-singapore:uid/${data.tencentcloud_sts_caller_identity.current.account_id}:${var.project}-*"]
          },
        ]
      }
    }

    # Read the SSH private key from SSM (e.g. for the Ansible-over-SSH mode).
    "builder-ssm-read" = {
      name        = "${local.name_prefix}-policy-builder-ssm-read"
      description = "Allow builder to read SSH key secret(s) from SSM"
      document = {
        version = "2.0"
        statement = [
          {
            effect = "allow"
            action = [
              "name/ssm:GetSecretValue",
              "name/ssm:DescribeSecret",
              "name/ssm:ListSecretVersionIds",
            ]
            resource = ["*"]
          },
        ]
      }
    }

    # TAT — run/inspect automation commands (pkrvars render). Mainly used by the
    # Terraform principal; granted here too so the builder can self-invoke.
    "builder-tat" = {
      name        = "${local.name_prefix}-policy-builder-tat"
      description = "Allow TAT command create/invoke/describe for config automation"
      document = {
        version = "2.0"
        statement = [
          {
            effect = "allow"
            action = [
              "name/tat:CreateCommand",
              "name/tat:ModifyCommand",
              "name/tat:DescribeCommands",
              "name/tat:InvokeCommand",
              "name/tat:RunCommand",
              "name/tat:DescribeInvocations",
              "name/tat:DescribeInvocationTasks",
            ]
            resource = ["*"]
          },
        ]
      }
    }
  }

  roles = {
    "builder" = {
      name             = "${local.name_prefix}-role-builder"
      description      = "CAM role for CIS image builder CVM — allows Packer image builds"
      trusted_services = ["cvm.qcloud.com"]
      console_login    = false
      policy_ids       = []
      custom_policy_keys = [
        "builder-cvm-packer",
        "builder-cos-readwrite",
        "builder-ssm-read",
        "builder-tat",
      ]
    }
  }

  tags = local.common_tags
}
