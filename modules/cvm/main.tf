terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

resource "tencentcloud_instance" "this" {
  for_each = var.instances

  instance_name           = each.value.name
  availability_zone       = each.value.availability_zone
  image_id                = each.value.image_id
  instance_type           = each.value.instance_type
  subnet_id               = each.value.subnet_id
  vpc_id                  = var.vpc_id
  orderly_security_groups = each.value.security_group_ids

  system_disk_type = each.value.system_disk_type
  system_disk_size = each.value.system_disk_size

  allocate_public_ip         = false
  internet_max_bandwidth_out = 0

  # Data disks (optional) — used for LVM partitions on hardened images
  dynamic "data_disks" {
    for_each = each.value.data_disks != null ? each.value.data_disks : []
    content {
      data_disk_type        = data_disks.value.disk_type
      data_disk_size        = data_disks.value.disk_size
      data_disk_snapshot_id = lookup(data_disks.value, "snapshot_id", null)
    }
  }

  key_ids       = each.value.key_ids
  user_data     = each.value.user_data != null ? each.value.user_data : null
  cam_role_name = each.value.cam_role_name != null ? each.value.cam_role_name : null

  tags = merge(var.tags, { Role = each.key })
}
