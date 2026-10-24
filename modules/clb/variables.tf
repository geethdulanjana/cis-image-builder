# ─────────────────────────────────────────────────────────────────────────────
# clb module — inputs
#
# A general-purpose Tencent Cloud Load Balancer module. Supports:
#   - OPEN (public) or INTERNAL (VPC-private) CLB
#   - multiple listeners (TCP/UDP L4, HTTP/HTTPS L7) with health checks
#   - attaching CVM backends to L4 listeners (targets)
#
# Designed to be reused: the CIS builder app today, TKE workloads later.
# L7 host/path rule routing and certificate wiring are intentionally left to
# the caller via dedicated resources when needed — this module covers the
# common L4 "front a set of CVMs" case plus plain L7 listeners.
# ─────────────────────────────────────────────────────────────────────────────

variable "clb_name" {
  description = "Name of the CLB instance."
  type        = string
}

variable "network_type" {
  description = "CLB network type: INTERNAL (VPC-private) or OPEN (public)."
  type        = string
  default     = "INTERNAL"

  validation {
    condition     = contains(["INTERNAL", "OPEN"], var.network_type)
    error_message = "network_type must be INTERNAL or OPEN."
  }
}

variable "vpc_id" {
  description = "VPC ID the CLB is created in."
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID for an INTERNAL CLB (required when network_type = INTERNAL; ignored for OPEN)."
  type        = string
  default     = null
}

variable "project_id" {
  description = "Tencent project id for the CLB."
  type        = number
  default     = 0
}

variable "security_groups" {
  description = "Security group ids to attach to the CLB (applies to OPEN CLBs; also supported for INTERNAL where enabled)."
  type        = list(string)
  default     = []
}

variable "sla_type" {
  description = "CLB spec/SLA type (e.g. clb.c2.medium). Empty = shared/default spec."
  type        = string
  default     = ""
}

variable "pass_to_target" {
  description = <<EOT
"Bypass backend security group." When true, only the CLB's own security group
gates access and the backend CVM's SG is NOT checked for CLB-forwarded traffic.
Set true for a public CLB that preserves the client source IP (otherwise that IP
is checked against the backend SG and dropped). Access control remains enforced
by the CLB security group.
EOT
  type        = bool
  default     = false
}

variable "listeners" {
  description = <<EOT
Map of listeners to create. Key is a logical name. Each object:
  - port       : listener port (1-65535)
  - protocol   : TCP | UDP | HTTP | HTTPS
  - scheduler  : WRR | LEAST_CONN | IP_HASH (default WRR)
  - session_expire_time : sticky-session seconds (0 = off; L4 only)

  Health check (mainly for L4 TCP/UDP):
  - health_check_switch        : enable health checks (default true)
  - health_check_type          : TCP | HTTP | CUSTOM
  - health_check_port          : override probe port (default = listener port)
  - health_check_http_path     : path for HTTP checks (default "/")
  - health_check_http_method   : GET | HEAD (default HEAD)
  - health_check_http_code     : bitmask of accepted status classes (1-31):
                                  1=1xx 2=2xx 4=3xx 8=4xx 16=5xx; sum them.
                                  Default 31 = accept all (never fail on code).
  - health_check_interval_time : seconds between probes (default 5)
  - health_check_time_out      : probe timeout seconds (default 2)
  - health_check_health_num    : successes to mark healthy (default 3)
  - health_check_unhealth_num  : failures to mark unhealthy (default 3)

  For HTTPS listeners:
  - certificate_ssl_mode : UNIDIRECTIONAL | MUTUAL
  - certificate_id       : server certificate id

  Backends (L4 only — attach CVMs directly to the listener):
  - targets : list of { instance_id, port, weight } (weight default 10)
EOT
  type = map(object({
    port                       = number
    protocol                   = string
    scheduler                  = optional(string, "WRR")
    session_expire_time        = optional(number, 0)
    health_check_switch        = optional(bool, true)
    health_check_type          = optional(string, "TCP")
    health_check_port          = optional(number)
    health_check_http_path     = optional(string, "/")
    health_check_http_method   = optional(string, "HEAD")
    health_check_http_code     = optional(number, 31)
    health_check_interval_time = optional(number, 5)
    health_check_time_out      = optional(number, 2)
    health_check_health_num    = optional(number, 3)
    health_check_unhealth_num  = optional(number, 3)
    certificate_ssl_mode       = optional(string)
    certificate_id             = optional(string)
    targets = optional(list(object({
      instance_id = string
      port        = number
      weight      = optional(number, 10)
    })), [])
  }))
  default = {}
}

variable "tags" {
  description = "Tags applied to the CLB instance."
  type        = map(string)
  default     = {}
}
