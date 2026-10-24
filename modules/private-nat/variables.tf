

variable "nat_gateways" {
  description = "Map of private NAT Gateway definitions. Key = AZ shortname (e.g. '1a', '2b')."
  type = map(object({
    name   = string
    ccn_id = string
  }))
}

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
