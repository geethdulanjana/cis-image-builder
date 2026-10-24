variable "vpc_id" {
  description = "VPC ID — from module.vpc.vpc_id"
  type        = string
}

variable "default_route_table_id" {
  description = "VPC default route table ID — from module.vpc.default_route_table_id. Used for subnets with route_table_key = 'default'."
  type        = string
}

variable "route_tables" {
  description = <<EOT
Map of route table definitions. Key is referenced by subnets via route_table_key.
Do NOT define "default" here — pass it via default_route_table_id instead.

Example:
  route_tables = {
    "pub-nat"   = { name = "proj-env-rt-pub-nat-1",   tags = { Tier = "public" } }
    "pvt-eks-1" = { name = "proj-env-rt-pvt-eks-1",   tags = { Tier = "private", AZ = "1a" } }
  }
EOT
  type = map(object({
    name = string
    tags = map(string)
  }))
  default = {}
}

# Subnet maps passed from the VPC module outputs — used for RT associations
variable "public_subnets" {
  description = "Public subnet map from module.vpc.public_subnets — includes id and route_table_key"
  type = map(object({
    id                = string
    cidr              = string
    availability_zone = string
    route_table_key   = string
  }))
  default = {}
}

variable "private_subnets" {
  description = "Private subnet map from module.vpc.private_subnets — includes id and route_table_key"
  type = map(object({
    id                = string
    cidr              = string
    availability_zone = string
    route_table_key   = string
  }))
  default = {}
}

variable "tags" {
  description = "Common tags merged into all route tables"
  type        = map(string)
  default     = {}
}
