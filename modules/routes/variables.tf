variable "rt_id_map" {
  description = "Map of route table key → route table ID. Pass module.route_tables.rt_id_map here."
  type        = map(string)
}

variable "routes" {
  description = <<EOT
List of route entries. Each route targets a route_table_key from rt_id_map.

Supported next_type values:
  - NAT        : NAT gateway
  - EIP        : EIP (next_hub = EIP address)
  - PEERCONNECTION : VPC peering connection
  - DIRECTCONNECT  : Direct Connect gateway
  - CCN        : Cloud Connect Network (TGW)
  - VPNGW      : VPN gateway
  - SSLVPNGW   : SSL VPN gateway
  - NORMAL_CVM : CVM instance (next_hub = CVM private IP)
  - LOCAL_GATEWAY : local gateway

Example:
  routes = [
    {
      route_table_key = "pvt-nat"
      destination     = "0.0.0.0/0"
      next_type       = "NAT"
      next_hub        = "nat-xxxxxxxx"
      description     = "Internet via NAT GW"
    },
    {
      route_table_key = "pvt-eks-1"
      destination     = "10.100.0.0/16"
      next_type       = "CCN"
      next_hub        = "ccn-xxxxxxxx"
      description     = "To shared services via TGW"
    }
  ]
EOT
  type = list(object({
    route_table_key = string
    destination     = string
    next_type       = string
    next_hub        = string
    description     = optional(string, "")
  }))
  default = []
}
