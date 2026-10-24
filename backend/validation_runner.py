#!/usr/bin/env python3
"""
validation_runner.py — end-to-end validation of a hardened image via TAT.

The CIS-hardened image blocks root SSH, so the Packer/SSH approach cannot log
in. Instead this uses Tencent Cloud APIs + TAT (the hardened image ships the TAT
agent, installed during the build):

  1. tccli cvm RunInstances   -> boot a throwaway CVM from the chosen image
  2. poll DescribeInstances   -> wait for RUNNING
  3. poll tat DescribeAutomationAgentStatus -> wait for the TAT agent online
  4. tat InvokeCommand (reboot) -> activate reboot-dependent CIS controls
  5. wait for agent back online
  6. tat InvokeCommand (health + OpenSCAP audit) -> capture stdout markers
  7. parse VAL_SCORE / VAL_PASS / VAL_FAIL / HEALTH markers
  8. tccli cvm TerminateInstances -> ALWAYS tear down (finally)

No SSH anywhere, and no transient image is created (we don't snapshot), so
there's nothing to delete and no CreateImage tag-policy concern.

Runs ON the builder CVM (needs tccli + the CAM role). Import-safe; use dry_run
for local testing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

import build_runner
from build_runner import PIPELINE_DIR, refresh_credentials, _read_main_pkrvars

LOG_DIR = os.environ.get("CIS_BUILD_LOG_DIR", os.path.join(PIPELINE_DIR, "logs"))
REGION = os.environ.get("TENCENTCLOUD_REGION", "ap-singapore")

# Optional COS destination for the OpenSCAP HTML report (base URL + optional
# presigned PUT). Set via env on the app service when COS is wired.
VAL_COS_BASE = os.environ.get("CIS_VAL_COS_BASE", "").rstrip("/")  # https://<bucket>.cos.<region>.myqcloud.com/validations
VAL_COS_PUT = os.environ.get("CIS_VAL_COS_PUT", "")               # optional presigned PUT url template

# Timeouts (seconds)
RUNNING_TIMEOUT = 300
AGENT_TIMEOUT = 300
REBOOT_SETTLE = 60

_procs_lock = threading.Lock()
_cancelled = set()


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _tccli(env, *args, timeout=120):
    """Run a tccli command, return (ok, parsed_json_or_text, raw)."""
    cmd = ["tccli", *args, "--region", REGION, "--output", "json"]
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, None, "tccli not found on PATH"
    except subprocess.TimeoutExpired:
        return False, None, f"tccli timed out: {' '.join(args[:2])}"
    raw = (out.stdout or "") + (out.stderr or "")
    if out.returncode != 0:
        return False, None, raw.strip()[:500]
    try:
        return True, json.loads(out.stdout), raw
    except (ValueError, json.JSONDecodeError):
        return True, None, raw


def run_validation(validation_id: str, image_id: str, on_update=None,
                   dry_run: bool = False, label: str = "", distro: str = "") -> dict:
    """Validate one hardened image end-to-end via TAT. Returns the final record."""
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(LOG_DIR, f"validation-{validation_id}-{ts}.log")

    record = {
        "validation_id": validation_id,
        "image_id": image_id,
        "label": label or image_id,
        "distro": distro or None,
        "status": "preparing",
        "log_path": log_path,
        "created_at": _utcnow(),
        "started_at": _utcnow(),
        "finished_at": None,
        "instance_id": None,
        "health": None,
        "audit": None,
        "reason": None,
        "error": None,
    }

    def update(**kw):
        record.update(kw)
        if on_update:
            on_update(dict(record))

    def log(text):
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    def cancelled():
        with _procs_lock:
            return validation_id in _cancelled

    if not re.fullmatch(r"img-[0-9a-z]{6,}", image_id or ""):
        update(status="failed", reason="invalid image id",
               error=f"invalid image id: {image_id!r}", finished_at=_utcnow())
        return record

    open(log_path, "w", encoding="utf-8").close()
    log(f"=== validation {validation_id} for image {image_id} — {_utcnow()} ===")
    update(status="preparing")

    if dry_run:
        log("DRY RUN — simulated validation")
        update(status="succeeded", reason="succeeded (dry-run), OpenSCAP 92%",
               health={"healthy": True, "system_running": "running", "failed_units": 0},
               audit={"available": False, "score": 92.0, "passed": 300, "failed": 26,
                      "note": "dry-run simulated"},
               finished_at=_utcnow())
        return record

    env = dict(os.environ)
    try:
        refresh_credentials(env)
    except Exception as e:  # noqa: BLE001
        update(status="failed", reason="credential refresh failed",
               error=str(e), finished_at=_utcnow())
        return record
    env["TENCENTCLOUD_REGION"] = REGION

    net = _read_main_pkrvars()
    zone = net.get("zone") or f"{REGION}-1"
    instance_type = net.get("instance_type") or "S5.MEDIUM2"
    vpc_id = net.get("vpc_id")
    subnet_id = net.get("subnet_id")
    sg_id = net.get("security_group_id")
    if not (vpc_id and subnet_id and sg_id):
        update(status="failed", reason="missing network config",
               error="vpc_id/subnet_id/security_group_id not found in pkrvars",
               finished_at=_utcnow())
        return record

    instance_id = None
    update(status="running")
    try:
        # 1. Boot a throwaway CVM from the hardened image, with mandatory tags.
        tag_spec = [{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "dialog:Environment", "Value": net.get("environment", "cis")},
                {"Key": "dialog:Project", "Value": net.get("project", "cis-platform")},
                {"Key": "dialog:Department", "Value": "IT"},
                {"Key": "dialog:CostCenter", "Value": "Cloudops"},
                {"Key": "dialog:DataClassification", "Value": "Cloudops"},
                {"Key": "dialog:Owner", "Value": "Geeth.Dulanjana"},
                {"Key": "dialog:ApplicationName", "Value": net.get("project", "cis-platform")},
                {"Key": "dialog:Budgetcode", "Value": "Cloudops"},
                {"Key": "dialog:CxoName", "Value": "IT"},
                {"Key": "ManagedBy", "Value": "cis-image-builder"},
                {"Key": "Purpose", "Value": "cis-validation-DELETE-ME"},
            ],
        }]
        placement = {"Zone": zone}
        vpc = {"VpcId": vpc_id, "SubnetId": subnet_id}
        sys_disk = {"DiskType": "CLOUD_SSD", "DiskSize": 50}
        log(f"RunInstances: image={image_id} type={instance_type} zone={zone}")
        ok, data, raw = _tccli(
            env, "cvm", "RunInstances",
            "--ImageId", image_id,
            "--InstanceType", instance_type,
            "--Placement", json.dumps(placement),
            "--VirtualPrivateCloud", json.dumps(vpc),
            "--SecurityGroupIds", json.dumps([sg_id]),
            "--SystemDisk", json.dumps(sys_disk),
            "--InstanceCount", "1",
            "--InstanceName", f"cis-validate-{validation_id}",
            "--TagSpecification", json.dumps(tag_spec),
            timeout=120,
        )
        if not ok or not data:
            update(status="failed", reason="RunInstances failed",
                   error=raw[:400], finished_at=_utcnow())
            return record
        ids = data.get("InstanceIdSet") or []
        if not ids:
            update(status="failed", reason="no instance id returned",
                   error=json.dumps(data)[:400], finished_at=_utcnow())
            return record
        instance_id = ids[0]
        record["instance_id"] = instance_id
        update(instance_id=instance_id)
        log(f"booted instance {instance_id}")

        # 2. Wait for RUNNING.
        if not _wait_running(env, instance_id, log, cancelled):
            update(status="failed", reason="instance did not reach RUNNING",
                   error="timeout waiting for RUNNING", finished_at=_utcnow())
            return record
        log("instance RUNNING")

        # 3. Wait for the TAT agent online.
        if not _wait_agent(env, instance_id, log, cancelled):
            update(status="failed", reason="TAT agent never came online",
                   error="is the agent baked into the image?", finished_at=_utcnow())
            return record
        log("TAT agent online")

        # 4. Reboot to activate reboot-dependent CIS controls.
        log("rebooting via TAT to activate reboot-dependent controls")
        _tat_run(env, instance_id, "sudo systemctl reboot || sudo reboot || true",
                 log, timeout=60)
        time.sleep(REBOOT_SETTLE)
        if not _wait_agent(env, instance_id, log, cancelled):
            update(status="failed", reason="agent did not return after reboot",
                   error="post-reboot agent timeout", finished_at=_utcnow())
            return record
        log("agent back after reboot")

        # 5. Health smoke-check via TAT.
        health_cmd = (
            'echo HEALTH system_running=$(systemctl is-system-running 2>/dev/null || echo unknown); '
            'echo HEALTH failed_units=$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null | wc -l); '
            'echo HEALTH uptime="$(uptime -p 2>/dev/null || uptime)"'
        )
        hout = _tat_run(env, instance_id, health_cmd, log, timeout=90) or ""
        health = _parse_health(hout)
        update(health=health)

        # 6. OpenSCAP audit via TAT. The audit script lives in the hardened image?
        #    No — it is not in the image. Ship it inline. The hardened image has
        #    oscap + ssg baked in (same package set as the builder base).
        audit_script = _audit_script_inline(validation_id)
        log("running OpenSCAP audit via TAT (several minutes)")
        aout = _tat_run(env, instance_id, audit_script, log, timeout=1200) or ""
        audit = _parse_audit(aout)
        update(audit=audit)

        healthy = health.get("healthy", False)
        if healthy:
            reason = "validated: image boots"
            if health.get("system_running"):
                reason += f" (systemd {health['system_running']})"
            if audit.get("score") is not None:
                reason += f", OpenSCAP {round(audit['score'])}%"
            update(status="succeeded", reason=reason, finished_at=_utcnow())
        else:
            update(status="failed",
                   reason="unhealthy: instance did not boot cleanly",
                   finished_at=_utcnow())
        return record

    except Exception as e:  # noqa: BLE001
        update(status="failed", reason=f"error: {str(e)[:120]}",
               error=str(e), finished_at=_utcnow())
        return record
    finally:
        # ALWAYS terminate the throwaway CVM.
        if instance_id:
            log(f"terminating validation instance {instance_id}")
            ok, _, raw = _tccli(env, "cvm", "TerminateInstances",
                                "--InstanceIds", json.dumps([instance_id]), timeout=120)
            log("terminated" if ok else f"WARNING: terminate failed: {raw[:200]}")
        with _procs_lock:
            _cancelled.discard(validation_id)


def _wait_running(env, instance_id, log, cancelled):
    deadline = time.time() + RUNNING_TIMEOUT
    while time.time() < deadline:
        if cancelled():
            return False
        ok, data, _ = _tccli(env, "cvm", "DescribeInstances",
                             "--InstanceIds", json.dumps([instance_id]), timeout=60)
        if ok and data:
            iset = data.get("InstanceSet") or []
            if iset:
                state = iset[0].get("InstanceState")
                if state == "RUNNING":
                    return True
                log(f"  instance state: {state}")
        time.sleep(8)
    return False


def _wait_agent(env, instance_id, log, cancelled):
    deadline = time.time() + AGENT_TIMEOUT
    while time.time() < deadline:
        if cancelled():
            return False
        ok, data, _ = _tccli(env, "tat", "DescribeAutomationAgentStatus",
                             "--InstanceIds", json.dumps([instance_id]), timeout=60)
        if ok and data:
            aset = data.get("AutomationAgentSet") or []
            if aset and aset[0].get("AgentStatus") == "Online":
                return True
        time.sleep(8)
    return False


def _tat_run(env, instance_id, content, log, timeout=300):
    """Invoke an inline shell command via TAT, wait for completion, return the
    combined command output (stdout). Best-effort: returns '' on failure."""
    import base64
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    ok, data, raw = _tccli(
        env, "tat", "RunCommand",
        "--Content", b64,
        "--InstanceIds", json.dumps([instance_id]),
        "--CommandType", "SHELL",
        "--Username", "root",
        "--Timeout", str(timeout),
        timeout=120,
    )
    if not ok or not data:
        log(f"  TAT RunCommand failed: {raw[:200]}")
        return ""
    invocation_id = data.get("InvocationId")
    if not invocation_id:
        log(f"  TAT: no InvocationId ({json.dumps(data)[:150]})")
        return ""

    # Poll the invocation task until finished, then fetch its output.
    deadline = time.time() + timeout + 60
    while time.time() < deadline:
        ok, inv, _ = _tccli(env, "tat", "DescribeInvocations",
                           "--InvocationIds", json.dumps([invocation_id]), timeout=60)
        state = None
        if ok and inv:
            iset = inv.get("InvocationSet") or []
            if iset:
                state = iset[0].get("InvocationStatus")
        if state in ("SUCCESS", "FAILED", "TIMEOUT", "PARTIAL_FAILED"):
            break
        time.sleep(6)

    # Fetch task output (base64-encoded).
    ok, tasks, _ = _tccli(env, "tat", "DescribeInvocationTasks",
                         "--Filters", json.dumps([{"Name": "invocation-id", "Values": [invocation_id]}]),
                         timeout=60)
    out_text = ""
    if ok and tasks:
        for t in (tasks.get("InvocationTaskSet") or []):
            res = t.get("TaskResult") or {}
            b = res.get("Output")
            if b:
                try:
                    out_text += base64.b64decode(b).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    out_text += str(b)
    if out_text:
        log("  --- TAT output ---")
        for line in out_text.splitlines():
            log("  " + line)
    return out_text


def _audit_script_inline(validation_id):
    """The OpenSCAP audit as an inline script for TAT. Emits VAL_* markers on
    stdout; optionally uploads the HTML report to COS when configured."""
    cos_env = ""
    if VAL_COS_BASE:
        url = f"{VAL_COS_BASE}/{validation_id}/report.html"
        cos_env = f'export CIS_VAL_COS_URL="{url}"\n'
        if VAL_COS_PUT:
            cos_env += f'export CIS_VAL_COS_PUT="{VAL_COS_PUT}"\n'
    # Reuse the canonical audit script shipped in the pipeline on the builder;
    # but the TARGET is the validation CVM, so we inline a self-contained scan.
    return cos_env + r"""
set -uo pipefail
OUT=/var/log/openscap; mkdir -p "$OUT"
command -v oscap >/dev/null 2>&1 || sudo dnf install -y openscap-scanner scap-security-guide || true
. /etc/os-release; MAJOR="${VERSION_ID%%.*}"
DS=""; for c in /usr/share/xml/scap/ssg/content/ssg-rhel${MAJOR}-ds.xml /usr/share/xml/scap/ssg/content/ssg-rhel${MAJOR}-ds-1.2.xml; do [ -f "$c" ] && DS="$c" && break; done
if [ -z "$DS" ]; then echo "VAL_SCORE=0"; echo "VAL_PASS=0"; echo "VAL_FAIL=0"; echo "VAL_DONE=1"; exit 0; fi
PROFILE=""; for p in xccdf_org.ssgproject.content_profile_cis xccdf_org.ssgproject.content_profile_cis_server_l1; do oscap info "$DS" 2>/dev/null | grep -q "$p" && PROFILE="$p" && break; done
[ -z "$PROFILE" ] && PROFILE=$(oscap info "$DS" 2>/dev/null | grep -oE 'xccdf_org\.ssgproject\.content_profile_[A-Za-z0-9_]*cis[A-Za-z0-9_]*' | head -1)
export TMPDIR=/root
oscap xccdf eval --profile "$PROFILE" --results "$OUT/results.xml" --report "$OUT/report.html" "$DS" || true
SCORE=$(grep -oE '<score[^>]*>[0-9.]+' "$OUT/results.xml" 2>/dev/null | grep -oE '[0-9.]+$' | head -1); [ -z "$SCORE" ] && SCORE=0
PASS=$(grep -c '<result>pass</result>' "$OUT/results.xml" 2>/dev/null || echo 0)
FAIL=$(grep -c '<result>fail</result>' "$OUT/results.xml" 2>/dev/null || echo 0)
echo "VAL_SCORE=${SCORE}"; echo "VAL_PASS=${PASS}"; echo "VAL_FAIL=${FAIL}"
if [ -n "${CIS_VAL_COS_URL:-}" ] && [ -f "$OUT/report.html" ]; then
  if command -v coscli >/dev/null 2>&1; then coscli cp "$OUT/report.html" "$CIS_VAL_COS_URL" && echo "VAL_REPORT_URL=${CIS_VAL_COS_URL}";
  elif [ -n "${CIS_VAL_COS_PUT:-}" ]; then curl -fsS -X PUT -T "$OUT/report.html" "$CIS_VAL_COS_PUT" && echo "VAL_REPORT_URL=${CIS_VAL_COS_URL}"; fi
fi
echo "VAL_DONE=1"
"""


def _parse_health(text: str) -> dict:
    health = {"system_running": None, "failed_units": None, "uptime": None,
              "ssh_back": None}
    m = re.search(r"HEALTH system_running=(\S+)", text)
    if m:
        health["system_running"] = m.group(1)
    m = re.search(r"HEALTH failed_units=(\d+)", text)
    if m:
        health["failed_units"] = int(m.group(1))
    m = re.search(r"HEALTH uptime=(.+)", text)
    if m:
        health["uptime"] = m.group(1).strip()
    sr = health["system_running"]
    health["healthy"] = sr in ("running", "degraded", "starting")
    return health


def _parse_audit(text: str) -> dict:
    out = {"available": False, "score": None, "passed": None, "failed": None,
           "report_url": None}
    m = re.search(r"VAL_SCORE=([0-9.]+)", text)
    if m:
        try:
            out["score"] = float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"VAL_PASS=(\d+)", text)
    if m:
        out["passed"] = int(m.group(1))
    m = re.search(r"VAL_FAIL=(\d+)", text)
    if m:
        out["failed"] = int(m.group(1))
    m = re.search(r"VAL_REPORT_URL=(\S+)", text)
    if m:
        out["report_url"] = m.group(1)
        out["available"] = True
    return out


def cancel_validation(validation_id: str, mark_status_cb=None) -> bool:
    with _procs_lock:
        _cancelled.add(validation_id)
    if mark_status_cb:
        mark_status_cb("canceling")
    return True


def is_running(validation_id: str) -> bool:
    # We can't easily introspect; report based on cancellation set absence is
    # not meaningful, so the API uses the store status instead. Kept for parity.
    return False
