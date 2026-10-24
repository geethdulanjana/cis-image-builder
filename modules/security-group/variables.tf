
# ─── SECURITY GROUPS ──────────────────────────────────────────────────────────
variable "security_groups" {
  description = <<EOT
Map of security group definitions. Key = logical name used for referencing.
Each object:
  - name       : SG display name
  - description: Human-readable purpose
  - ingress    : list of ingress rules
  - egress     : list of egress rules

Rule object fields:
  - protocol    : "TCP" | "UDP" | "ICMP" | "ALL"
  - port        : port or range e.g. "22" or "8080-8090" or "ALL"
  - cidr_block  : source/destination CIDR
  - description : rule description
EOT
  type = map(object({
    name        = string
    description = string
    ingress = list(object({
      protocol    = string
      port        = string
      cidr_block  = string
      description = string
      action      = string
    }))
    egress = list(object({
      protocol    = string
      port        = string
      cidr_block  = string
      description = string
      action      = string
    }))
  }))
  default = {}
}

# ─── TAGS ─────────────────────────────────────────────────────────────────────
variable "tags" {
  description = "Common tags applied to all resources. Recommended: Environment, Project, ManagedBy."
  type        = map(string)
  default     = {}
}
