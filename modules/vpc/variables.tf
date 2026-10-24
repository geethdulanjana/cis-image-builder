# ─── VPC ──────────────────────────────────────────────────────────────────────
variable "vpc_name" {
  description = "Name of the VPC"
  type        = string
}

variable "vpc_cidr" {
  description = "Primary CIDR block for the VPC. Keep this wide (e.g. /16) — Tencent Cloud peering only supports the primary CIDR."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be a valid CIDR block."
  }
}

variable "assistant_cidrs" {
  description = "Secondary/assistant CIDR blocks for the VPC (e.g. [\"172.16.0.0/24\"]). Note: these are NOT routable over VPC peering — primary CIDR only."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.assistant_cidrs : can(cidrnetmask(cidr))])
    error_message = "All assistant_cidrs must be valid CIDR blocks e.g. [\"172.16.0.0/24\"]."
  }
}

# ─── SUBNETS ──────────────────────────────────────────────────────────────────
variable "public_subnets" {
  description = <<EOT
List of public subnet definitions. Each object:
  - name             : subnet name
  - cidr             : CIDR block (must be within vpc_cidr)
  - availability_zone: AZ e.g. "ap-singapore-1"
  - route_table_key  : Key for route table association
EOT
  type = list(object({
    name              = string
    cidr              = string
    availability_zone = string
    route_table_key   = string
  }))
  default = []
}

variable "private_subnets" {
  description = "List of private subnet definitions. Same schema as public_subnets."
  type = list(object({
    name              = string
    cidr              = string
    availability_zone = string
    route_table_key   = string
  }))
  default = []
}
# ─── TAGS ─────────────────────────────────────────────────────────────────────
variable "tags" {
  description = "Common tags applied to all resources. Recommended: Environment, Project, ManagedBy."
  type        = map(string)
  default     = {}
}
