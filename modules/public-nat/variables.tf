# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC NAT GATEWAY MODULE — Multi-instance design
#
# Accepts a map of NAT gateway definitions — one entry per NAT GW.
# Currently used with a single entry (1a) but can scale to per-AZ
# by adding more entries to the map in envs/dev/main.tf.
#
# Example — current (single):
#   nat_gateways = {
#     "1a" = { eip_name = "...", ... }
#   }
#
# Example — future (per-AZ):
#   nat_gateways = {
#     "1a" = { eip_name = "...", ... }
#     "2b" = { eip_name = "...", ... }
#     "3c" = { eip_name = "...", ... }
#   }
# ─────────────────────────────────────────────────────────────────────────────

variable "vpc_id" {
  description = "VPC ID — from module.vpc.vpc_id"
  type        = string
}

variable "nat_gateways" {
  description = "Map of public NAT Gateway definitions. Key = AZ shortname (e.g. '1a', '2b')."
  type = map(object({
    name          = string
    eip_name      = string
    eip_isp       = string
    eip_bandwidth = number
  }))

  validation {
    condition = alltrue([
      for k, v in var.nat_gateways :
      contains(["BGP", "CMCC", "CTCC", "CUCC"], v.eip_isp)
    ])
    error_message = "eip_isp must be one of: BGP, CMCC, CTCC, CUCC."
  }
}

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
