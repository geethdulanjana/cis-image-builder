# ─────────────────────────────────────────────────────────────────────────────
# clb module — Tencent Cloud Load Balancer (general purpose)
#
# Creates one CLB instance, N listeners, and (for L4 listeners) attaches CVM
# backends. Reusable for the CIS builder app and future TKE workloads.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_providers {
    tencentcloud = {
      source  = "tencentcloudstack/tencentcloud"
      version = "~> 1.83"
    }
  }
}

locals {
  # L4 (TCP/UDP) listeners can carry health-check + backend targets directly.
  l4_protocols = ["TCP", "UDP"]

  # Flatten listener -> targets into a single map for clb_attachment, keyed by
  # "<listener_key>:<instance_id>:<port>" so each attachment is addressable.
  attachments = merge([
    for lk, l in var.listeners : {
      for t in l.targets :
      "${lk}:${t.instance_id}:${t.port}" => {
        listener_key = lk
        instance_id  = t.instance_id
        port         = t.port
        weight       = t.weight
      }
    } if contains(local.l4_protocols, upper(l.protocol))
  ]...)
}

resource "tencentcloud_clb_instance" "this" {
  clb_name     = var.clb_name
  network_type = var.network_type
  vpc_id       = var.vpc_id
  subnet_id    = var.network_type == "INTERNAL" ? var.subnet_id : null
  project_id   = var.project_id

  security_groups = length(var.security_groups) > 0 ? var.security_groups : null
  sla_type        = var.sla_type != "" ? var.sla_type : null

  # "Bypass backend security group": when true, only the CLB's SG gates access;
  # the backend CVM's SG is NOT evaluated for CLB-forwarded traffic. Needed when
  # the CLB preserves the client's real source IP (which otherwise gets checked
  # against the backend SG and blocked). Access control stays on the CLB SG.
  load_balancer_pass_to_target = var.pass_to_target

  tags = var.tags
}

resource "tencentcloud_clb_listener" "this" {
  for_each = var.listeners

  clb_id        = tencentcloud_clb_instance.this.id
  listener_name = each.key
  port          = each.value.port
  protocol      = upper(each.value.protocol)
  scheduler     = each.value.scheduler

  # Session stickiness (L4 only; provider ignores for L7 here).
  session_expire_time = contains(local.l4_protocols, upper(each.value.protocol)) && each.value.session_expire_time > 0 ? each.value.session_expire_time : null

  # Health check — set for L4 listeners (and HTTP(S) that opt in).
  health_check_switch        = each.value.health_check_switch
  health_check_type          = each.value.health_check_switch ? each.value.health_check_type : null
  health_check_port          = each.value.health_check_switch ? coalesce(each.value.health_check_port, each.value.port) : null
  health_check_interval_time = each.value.health_check_switch ? each.value.health_check_interval_time : null
  health_check_time_out      = each.value.health_check_switch ? each.value.health_check_time_out : null
  health_check_health_num    = each.value.health_check_switch ? each.value.health_check_health_num : null
  health_check_unhealth_num  = each.value.health_check_switch ? each.value.health_check_unhealth_num : null

  # HTTP-type health probe details (used when health_check_type = HTTP).
  # health_check_http_code is a BITMASK (1-31), not an HTTP status number:
  # 1=1xx 2=2xx 4=3xx 8=4xx 16=5xx. Must be set for HTTP checks or the API
  # rejects the request ("HttpCode should be 1~31").
  health_check_http_path   = each.value.health_check_switch && upper(each.value.health_check_type) == "HTTP" ? each.value.health_check_http_path : null
  health_check_http_method = each.value.health_check_switch && upper(each.value.health_check_type) == "HTTP" ? each.value.health_check_http_method : null
  health_check_http_code   = each.value.health_check_switch && upper(each.value.health_check_type) == "HTTP" ? each.value.health_check_http_code : null

  # HTTPS termination (optional).
  certificate_ssl_mode = upper(each.value.protocol) == "HTTPS" ? each.value.certificate_ssl_mode : null
  certificate_id       = upper(each.value.protocol) == "HTTPS" ? each.value.certificate_id : null
}

# Attach CVM backends to L4 listeners.
resource "tencentcloud_clb_attachment" "this" {
  for_each = local.attachments

  clb_id      = tencentcloud_clb_instance.this.id
  listener_id = tencentcloud_clb_listener.this[each.value.listener_key].listener_id

  targets {
    instance_id = each.value.instance_id
    port        = each.value.port
    weight      = each.value.weight
  }
}
