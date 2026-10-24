#!/usr/bin/env python3
"""
validation_runner.py — end-to-end validation of a hardened image via SSH.

The CIS-hardened image blocks ROOT ssh and (in practice) TAT didn't come online,
so validation uses a baked, key-only, non-root sudo user 'cisval' that the build
puts into the image (scripts/06-add-validation-user.sh). The matching private
key lives in SSM (the Terraform-generated builder keypair).

Flow (no root login, no TAT dependency):
  1. tccli cvm RunInstances       -> boot a throwaway CVM from the image
  2. poll DescribeInstances       -> wait RUNNING + get private IP
  3. fetch private key from SSM   -> write a temp key file
  4. ssh cisval@ip                -> wait for SSH to answer
  5. sudo reboot; reconnect       -> activate reboot-dependent CIS controls
  6. health smoke + OpenSCAP      -> over ssh/sudo; parse markers
  7. scp report.html to builder   -> served via the report endpoint
  8. tccli cvm TerminateInstances -> ALWAYS tear down (finally)

Runs ON the builder CVM (needs tccli + ssh/scp + the CAM role). No snapshot
image is created. Import-safe; use dry_run for local testing.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

import build_runner
from build_runner import PIPELINE_DIR, refresh_credentials, _read_main_pkrvars

LOG_DIR = os.environ.get("CIS_BUILD_LOG_DIR", os.path.join(PIPELINE_DIR, "logs"))
REGION = os.environ.get("TENCENTCLOUD_REGION", "ap-singapore")
VAL_USER = os.environ.get("CIS_VAL_USER", "cisval")
# SSM secret holding the PEM private key (same keypair baked into the image).
VAL_KEY_SECRET = os.environ.get("CIS_VAL_KEY_SECRET", "cis-platform-shared-builder-ssh-key")
VAL_KEY_VERSION = os.environ.get("CIS_VAL_KEY_VERSION", "v1")
# Where validation OpenSCAP reports are stored on the builder (served by API).
VAL_REPORT_DIR = os.environ.get(
    "CIS_VAL_REPORT_DIR", os.path.join(PIPELINE_DIR, "audit-results", "validations"))

RUNNING_TIMEOUT = 300
SSH_TIMEOUT = 300
REBOOT_SETTLE = 45

_procs_lock = threading.Lock()
_cancelled = set()


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _tccli(env, *args, timeout=120):
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


def _fetch_private_key(env, log):
    """Get the PEM private key from SSM, write to a 0600 temp file, return path."""
    ok, data, raw = _tccli(env, "ssm", "GetSecretValue",
                           "--SecretName", VAL_KEY_SECRET,
                           "--VersionId", VAL_KEY_VERSION, timeout=60)
    if not ok or not data:
        log(f"  SSM GetSecretValue failed: {raw[:200]}")
        return None
    pem = data.get("SecretString") or ""
    if not pem:
        log("  SSM returned empty SecretString")
        return None
    fd, path = tempfile.mkstemp(prefix="cisval-", suffix=".pem")
    with os.fdopen(fd, "w") as f:
        f.write(pem if pem.endswith("\n") else pem + "\n")
    os.chmod(path, 0o600)
    return path


def _ssh_base(key_path, ip):
    return [
        "ssh", "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "LogLevel=ERROR",
        f"{VAL_USER}@{ip}",
    ]


def _ssh_run(key_path, ip, remote_cmd, timeout=600):
    """Run a remote command over ssh; return (rc, output)."""
    cmd = _ssh_base(key_path, ip) + [remote_cmd]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "ssh command timed out"


def _port_open(ip, port, log, attempts=10, delay=8):
    """TCP-connect probe: distinguishes 'sshd/network down' from 'auth rejected'."""
    import socket
    for i in range(attempts):
        try:
            with socket.create_connection((ip, port), timeout=5):
                return True
        except OSError as e:
            if i == 0 or i == attempts - 1:
                log(f"  port {port} probe {i+1}/{attempts}: {e}")
            time.sleep(delay)
    return False


def _wait_ssh(key_path, ip, log, cancelled, timeout=SSH_TIMEOUT):
    deadline = time.time() + timeout
    attempt = 0
    last = ""
    while time.time() < deadline:
        if cancelled():
            return False
        attempt += 1
        rc, out = _ssh_run(key_path, ip, "echo __ssh_ok__", timeout=15)
        if rc == 0 and "__ssh_ok__" in out:
            return True
        last = out.strip()
        # Log every few attempts so the reason is visible (auth vs refused vs
        # timeout), not just a silent spin to teardown.
        if attempt == 1 or attempt % 4 == 0:
            log(f"  ssh attempt {attempt} rc={rc}: {last[:200]}")
        time.sleep(8)
    log(f"  ssh gave up after {attempt} attempts. last error: {last[:300]}")
    return False


def run_validation(validation_id: str, image_id: str, on_update=None,
                   dry_run: bool = False, label: str = "", distro: str = "") -> dict:
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(LOG_DIR, f"validation-{validation_id}-{ts}.log")

    record = {
        "validation_id": validation_id, "image_id": image_id,
        "label": label or image_id, "distro": distro or None,
        "status": "preparing", "log_path": log_path,
        "created_at": _utcnow(), "started_at": _utcnow(), "finished_at": None,
        "instance_id": None, "health": None, "audit": None,
        "reason": None, "error": None,
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
    vpc_id, subnet_id, sg_id = net.get("vpc_id"), net.get("subnet_id"), net.get("security_group_id")
    if not (vpc_id and subnet_id and sg_id):
        update(status="failed", reason="missing network config",
               error="vpc_id/subnet_id/security_group_id not in pkrvars",
               finished_at=_utcnow())
        return record

    key_path = None
    instance_id = None
    update(status="running")
    try:
        # 1. Fetch the SSH private key from SSM.
        key_path = _fetch_private_key(env, log)
        if not key_path:
            update(status="failed", reason="could not fetch SSH key from SSM",
                   error="SSM key fetch failed", finished_at=_utcnow())
            return record

        # 2. Boot the throwaway CVM (with mandatory dialog:* tags).
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
        log(f"RunInstances: image={image_id} type={instance_type} zone={zone}")
        ok, data, raw = _tccli(
            env, "cvm", "RunInstances",
            "--ImageId", image_id, "--InstanceType", instance_type,
            "--Placement", json.dumps({"Zone": zone}),
            "--VirtualPrivateCloud", json.dumps({"VpcId": vpc_id, "SubnetId": subnet_id}),
            "--SecurityGroupIds", json.dumps([sg_id]),
            "--SystemDisk", json.dumps({"DiskType": "CLOUD_SSD", "DiskSize": 50}),
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

        # 3. Wait RUNNING + get private IP.
        ip = _wait_running_ip(env, instance_id, log, cancelled)
        if not ip:
            update(status="failed", reason="instance did not reach RUNNING",
                   error="timeout waiting for RUNNING/IP", finished_at=_utcnow())
            return record
        log(f"instance RUNNING at {ip}")

        # Diagnostics: key sanity + is port 22 even open on the box?
        try:
            ksz = os.path.getsize(key_path)
            log(f"ssh key: {key_path} ({ksz} bytes), user={VAL_USER}, target={ip}:22")
        except OSError:
            pass
        if _port_open(ip, 22, log):
            log("port 22 is OPEN (sshd reachable) — auth/user is the next gate")
        else:
            log("port 22 NOT reachable — network/SG/route or sshd not up yet")

        # 4. Wait for SSH as cisval.
        if not _wait_ssh(key_path, ip, log, cancelled):
            update(status="failed", reason="SSH never came up (cisval)",
                   error="see log: port-22/auth diagnostics above",
                   finished_at=_utcnow())
            return record
        log("SSH up as cisval")

        # 5. Reboot to activate reboot-dependent controls, then reconnect.
        log("rebooting to activate reboot-dependent controls")
        _ssh_run(key_path, ip, "sudo systemctl reboot || sudo reboot || true", timeout=30)
        time.sleep(REBOOT_SETTLE)
        if not _wait_ssh(key_path, ip, log, cancelled):
            update(status="failed", reason="SSH did not return after reboot",
                   error="post-reboot ssh timeout", finished_at=_utcnow())
            return record
        log("SSH back after reboot")

        # 6. Health smoke-check.
        rc, hout = _ssh_run(key_path, ip,
            'echo HEALTH system_running=$(systemctl is-system-running 2>/dev/null || echo unknown); '
            'echo HEALTH failed_units=$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null | wc -l); '
            'echo HEALTH uptime="$(uptime -p 2>/dev/null || uptime)"', timeout=60)
        log("  " + hout.replace("\n", "\n  "))
        health = _parse_health(hout)
        update(health=health)

        # 7. OpenSCAP audit (sudo; writes to /var/log/openscap).
        log("running OpenSCAP audit over ssh (several minutes)")
        rc, aout = _ssh_run(key_path, ip, "sudo bash -c " + _shq(_audit_script()), timeout=1500)
        for line in aout.splitlines():
            log("  " + line)
        audit = _parse_audit(aout)

        # 8. Pull the HTML report back to the builder (served by the API).
        os.makedirs(VAL_REPORT_DIR, exist_ok=True)
        dest = os.path.join(VAL_REPORT_DIR, f"{validation_id}.html")
        scp = ["scp", "-i", key_path, "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
               f"{VAL_USER}@{ip}:/tmp/openscap-report.html", dest]
        try:
            # copy report to a user-readable path first (sudo), then scp
            _ssh_run(key_path, ip, "sudo cp /var/log/openscap/report.html /tmp/openscap-report.html 2>/dev/null; "
                                   "sudo chmod 644 /tmp/openscap-report.html 2>/dev/null || true", timeout=30)
            r = subprocess.run(scp, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
                audit["available"] = True
                audit["report_path"] = dest
                log(f"report saved: {dest}")
            else:
                log(f"  report scp failed: {(r.stderr or '').strip()[:200]}")
        except Exception as e:  # noqa: BLE001
            log(f"  report fetch error: {e}")
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
        if instance_id:
            log(f"terminating validation instance {instance_id}")
            ok, _, raw = _tccli(env, "cvm", "TerminateInstances",
                                "--InstanceIds", json.dumps([instance_id]), timeout=120)
            log("terminated" if ok else f"WARNING: terminate failed: {raw[:200]}")
        if key_path and os.path.exists(key_path):
            try:
                os.remove(key_path)
            except OSError:
                pass
        with _procs_lock:
            _cancelled.discard(validation_id)


def _wait_running_ip(env, instance_id, log, cancelled):
    deadline = time.time() + RUNNING_TIMEOUT
    while time.time() < deadline:
        if cancelled():
            return None
        ok, data, _ = _tccli(env, "cvm", "DescribeInstances",
                             "--InstanceIds", json.dumps([instance_id]), timeout=60)
        if ok and data:
            iset = data.get("InstanceSet") or []
            if iset:
                state = iset[0].get("InstanceState")
                ips = iset[0].get("PrivateIpAddresses") or []
                if state == "RUNNING" and ips:
                    return ips[0]
                log(f"  state={state} ips={ips}")
        time.sleep(8)
    return None


def _shq(s: str) -> str:
    """Single-quote a string for safe use inside: sudo bash -c <this>."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _audit_script() -> str:
    """Self-contained OpenSCAP scan; prints VAL_* markers; leaves report at
    /var/log/openscap/report.html."""
    return r"""
set -uo pipefail
OUT=/var/log/openscap; mkdir -p "$OUT"
command -v oscap >/dev/null 2>&1 || dnf install -y openscap-scanner scap-security-guide || true
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
echo "VAL_SCORE=${SCORE}"; echo "VAL_PASS=${PASS}"; echo "VAL_FAIL=${FAIL}"; echo "VAL_DONE=1"
"""


def _parse_health(text: str) -> dict:
    health = {"system_running": None, "failed_units": None, "uptime": None}
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
           "report_path": None}
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
    return out


def cancel_validation(validation_id: str, mark_status_cb=None) -> bool:
    with _procs_lock:
        _cancelled.add(validation_id)
    if mark_status_cb:
        mark_status_cb("canceling")
    return True


def is_running(validation_id: str) -> bool:
    return False
