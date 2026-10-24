variable "policies" {
  description = <<EOT
Map of custom CAM policy definitions. Key = logical name.
Each object:
  - name        : policy display name
  - description : policy description
  - document    : policy document as HCL object (will be JSON-encoded)
                  Structure: { version, statement: [{ effect, action, resource }] }
EOT
  type = map(object({
    name        = string
    description = string
    document    = any
  }))
  default = {}
}

variable "roles" {
  description = <<EOT
Map of CAM role definitions. Key = logical name.
Each object:
  - name               : role display name
  - description        : role description
  - trusted_services   : list of services allowed to assume the role
                         e.g. ["cvm.qcloud.com"]
  - console_login      : whether sub-accounts can log in via console
  - policy_ids         : list of built-in CAM policy IDs to attach
                         Common built-in IDs:
                           ReadOnlyAccess        = 101
                           QcloudCVMFullAccess   = 2
                           QcloudCOSFullAccess   = 4
                           QcloudCLSFullAccess   = 575742
                           QcloudTKEFullAccess   = 1277936
  - custom_policy_keys : list of keys from var.policies to attach
EOT
  type = map(object({
    name               = string
    description        = string
    trusted_services   = list(string)
    console_login      = optional(bool, false)
    policy_ids         = optional(list(number), [])
    custom_policy_keys = optional(list(string), [])
  }))
  default = {}
}

variable "tags" {
  description = "Common tags applied to all roles"
  type        = map(string)
  default     = {}
}
