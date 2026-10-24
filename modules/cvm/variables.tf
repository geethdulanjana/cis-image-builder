variable "vpc_id" {
  description = "VPC ID to launch instances into"
  type        = string
}

variable "instances" {
  description = <<EOT
Map of CVM instance definitions. Key = logical name (e.g. "bastion-1a").
Each object:
  - name                : display name of the instance
  - availability_zone   : AZ e.g. "ap-singapore-1"
  - image_id            : CVM image ID e.g. "img-xxxxxx"
  - instance_type       : e.g. "SA5.MEDIUM2" (2C4G)
  - subnet_id           : subnet to place the instance in
  - security_group_ids  : list of security group IDs
  - system_disk_type    : "CLOUD_BSSD" | "CLOUD_PREMIUM" | "CLOUD_SSD"
  - system_disk_size    : disk size in GB (min 50)
  - allocate_public_ip  : whether to assign a public IP
  - internet_bandwidth  : max outbound Mbps when allocate_public_ip = true
  - key_ids             : list of SSH key pair IDs
EOT
  type = map(object({
    name               = string
    availability_zone  = string
    image_id           = string
    instance_type      = string
    subnet_id          = string
    security_group_ids = list(string)
    system_disk_type   = optional(string, "CLOUD_BSSD")
    system_disk_size   = optional(number, 50)
    key_ids            = optional(list(string), [])
    user_data          = optional(string)
    cam_role_name      = optional(string)
    data_disks = optional(list(object({
      disk_type   = string
      disk_size   = number
      snapshot_id = optional(string)
    })))
  }))
  default = {}
}

variable "tags" {
  description = "Common tags applied to all instances"
  type        = map(string)
  default     = {}
}
