#!/usr/bin/env python3
"""
validation_runner.py — end-to-end validation of a hardened image.

Boots a THROWAWAY CVM from a chosen hardened image, reboots it so
reboot-dependent CIS controls take effect, runs a health smoke-check, then runs
OpenSCAP to measure how many CIS controls actually pass on the running system.
Finally tears down the transient validation image.

This reuses the Packer `validate` template (packer/validate/validate.pkr.hcl) —
the same engine the build flow uses for post-boot verification — but as a
standalone, UI-triggered action against ANY image id.

Runs ON the builder CVM (needs packer + tccli + the CAM role, like build_runner).
Import-safe on any platform: nothing runs at import time. Use dry_run for local.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone

# Reuse the proven helpers/paths from build_runner so behavior stays consistent.
import build_runner
from build_runner import (
    PIPELINE_DIR,
    IMAGE_ID_RE,
    refresh_credentials,
    delete_image,
    _read_main_pkrvars,
    VALIDATE_DIR,
    AUDIT_STORE,
)

# Per-validation staging dir the validate template downloads into.
VALIDATE_STAGING = os.path.join(PIPELINE_DIR, "audit-results", "latest-validate")
LOG_DIR = os.environ.get("CIS_BUILD_LOG_DIR", os.path.join(PIPELINE_DIR, "logs"))

IMG_RE = re.compile(r"img-[0-9a-z]{6,}")

# Registry of running validation subprocesses so the API can cancel them.
_procs = {}
_procs_lock = threading.Lock()


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _parse_health(text: str) -> dict:
    """Extract health signals the validate template prints after reboot.

    The template emits marker lines (see validate.pkr.hcl):
      HEALTH system_running=<state>
      HEALTH failed_units=<n>
      HEALTH uptime=<...>
    Returns a dict + a boolean 'healthy'. Best-effort: missing markers -> unknown.
    """
    health = {
        "system_running": None,
        "failed_units": None,
        "uptime": None,
        "ssh_back": "Back after reboot" in text or "HEALTH" in text,
    }
    m = re.search(r"HEALTH system_running=(\S+)", text)
    if m:
        health["system_running"] = m.group(1)
    m = re.search(r"HEALTH failed_units=(\d+)", text)
    if m:
        health["failed_units"] = int(m.group(1))
    m = re.search(r"HEALTH uptime=(.+)", text)
    if m:
        health["uptime"] = m.group(1).strip()

    # "running" or "degraded" both mean systemd came up; only a hard failure or
    # no-SSH-back is unhealthy. degraded = up but some unit failed (still booted).
    sr = health["system_running"]
    healthy = bool(health["ssh_back"]) and sr in (None, "running", "degraded", "starting")
    health["healthy"] = healthy
    return health


def _parse_scap(results_dir: str) -> dict:
    """Parse score.txt + results.xml from a downloaded OpenSCAP results dir."""
    out = {"available": False, "score": None, "passed": None, "failed": None,
           "report_path": None, "results_dir": results_dir}
    score_file = os.path.join(results_dir, "score.txt")
    if os.path.exists(score_file):
        try:
            out["score"] = float(open(score_file, encoding="utf-8").read().strip())
        except (ValueError, OSError):
            pass
    rx = os.path.join(results_dir, "results.xml")
    if os.path.exists(rx):
        try:
            txt = open(rx, encoding="utf-8", errors="replace").read()
            out["passed"] = txt.count("<result>pass</result>")
            out["failed"] = txt.count("<result>fail</result>")
        except OSError:
            pass
    report = os.path.join(results_dir, "report.html")
    if os.path.exists(report):
        out["available"] = True
        out["report_path"] = report
    else:
        out["note"] = "no OpenSCAP report produced (scanner/content missing?)"
    return out


def cancel_validation(validation_id: str, mark_status_cb=None) -> bool:
    with _procs_lock:
        proc = _procs.get(validation_id)
    if proc is None:
        return False
    if mark_status_cb:
        mark_status_cb("canceling")
    try:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:  # noqa: BLE001
        return False
    return True


def is_running(validation_id: str) -> bool:
    with _procs_lock:
        return validation_id in _procs


def run_validation(validation_id: str, image_id: str, on_update=None,
                   dry_run: bool = False, label: str = "", distro: str = "") -> dict:
    """Validate one hardened image end-to-end. Returns the final record.

    Steps: boot throwaway CVM from image_id -> reboot -> health smoke -> OpenSCAP
    -> parse score -> ALWAYS delete the transient validation image.
    """
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
        "health": None,
        "audit": None,          # OpenSCAP result for the validated image
        "reason": None,
        "error": None,
    }

    def update(**kw):
        record.update(kw)
        if on_update:
            on_update(dict(record))

    def log_append(text):
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(text)
        except OSError:
            pass

    # Validate image id shape early.
    if not re.fullmatch(r"img-[0-9a-z]{6,}", image_id or ""):
        update(status="failed", reason="invalid image id",
               error=f"invalid image id: {image_id!r}", finished_at=_utcnow())
        return record

    update(status="preparing")

    if dry_run:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"DRY RUN — would validate image {image_id}\n")
        update(status="succeeded", reason="succeeded (dry-run)",
               health={"healthy": True, "system_running": "running",
                       "failed_units": 0, "ssh_back": True},
               audit={"available": False, "score": 92.0, "passed": 300,
                      "failed": 26, "note": "dry-run simulated"},
               finished_at=_utcnow())
        return record

    env = dict(os.environ)
    try:
        refresh_credentials(env)
    except Exception as e:  # noqa: BLE001
        update(status="failed", reason="credential refresh failed",
               error=str(e), finished_at=_utcnow())
        return record

    validate_dir = os.path.abspath(VALIDATE_DIR)
    if not os.path.isdir(validate_dir):
        update(status="failed", reason="validate dir missing",
               error=f"validate dir not found: {validate_dir}", finished_at=_utcnow())
        return record

    # Clear stale staging.
    if os.path.isdir(VALIDATE_STAGING):
        shutil.rmtree(VALIDATE_STAGING, ignore_errors=True)

    # Build the packer command: boot FROM the chosen image, reusing the main
    # pkrvars network settings. cwd = pipeline root so file-download + script
    # paths resolve (same as run_postboot_validation).
    net_vars = _read_main_pkrvars()
    cmd = ["packer", "build", "-var", f"hardened_image_id={image_id}"]
    # Pass network + governance vars from the main pkrvars so the transient
    # validation CVM/image carry the mandatory dialog:* tags (a CAM policy
    # denies resource creation when dialog:CxoName is absent).
    for k in ("region", "zone", "instance_type", "vpc_id", "subnet_id",
              "security_group_id", "environment", "project"):
        if net_vars.get(k):
            cmd += ["-var", f"{k}={net_vars[k]}"]
    cmd.append(validate_dir)

    pipeline_root = os.path.abspath(PIPELINE_DIR)
    if os.environ.get("CIS_PACKER_DEBUG", "0") == "1":
        env["PACKER_LOG"] = "1"
    else:
        env.pop("PACKER_LOG", None)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Validating image {image_id}\n")
        f.write(f"Running: {' '.join(cmd)} (cwd={pipeline_root})\n\n")

    update(status="running")
    captured = []
    transient_img = None
    try:
        proc = subprocess.Popen(
            cmd, cwd=pipeline_root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        with _procs_lock:
            _procs[validation_id] = proc
        assert proc.stdout is not None
        with open(log_path, "a", encoding="utf-8") as lf:
            for line in proc.stdout:
                lf.write(line)
                lf.flush()
                captured.append(line)
        proc.wait()

        full = "".join(captured)
        canceled = record.get("status") == "canceling"

        # The validate template always snapshots a transient image; find it so
        # we can delete it (any img- that isn't the one being validated).
        for cand in reversed(IMG_RE.findall(full)):
            if cand != image_id:
                transient_img = cand
                break

        if canceled:
            update(status="canceled", reason="canceled by user",
                   error="canceled by user", finished_at=_utcnow())
            return record

        # Parse health signals from the reboot/smoke output.
        health = _parse_health(full)
        update(health=health)

        if proc.returncode != 0:
            update(status="failed",
                   reason=build_runner.summarize_outcome(full, proc.returncode),
                   error=f"packer exited {proc.returncode}",
                   finished_at=_utcnow())
            return record

        # Move staged OpenSCAP results into a per-validation dir and parse.
        if not os.path.isdir(VALIDATE_STAGING):
            update(status="failed", reason="no OpenSCAP results produced",
                   error="validate staging dir missing after run",
                   finished_at=_utcnow())
            return record
        dest = os.path.join(AUDIT_STORE, f"validation-{validation_id}")
        try:
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            os.makedirs(AUDIT_STORE, exist_ok=True)
            shutil.move(VALIDATE_STAGING, dest)
        except OSError as e:
            update(status="failed", reason="could not store results",
                   error=str(e), finished_at=_utcnow())
            return record

        audit = _parse_scap(dest)
        healthy = health.get("healthy", False)
        reason = "validated: image boots"
        if health.get("system_running"):
            reason += f" (systemd {health['system_running']})"
        if audit.get("score") is not None:
            reason += f", OpenSCAP {round(audit['score'])}%"
        update(status="succeeded" if healthy else "failed",
               audit=audit,
               reason=reason if healthy else "unhealthy: instance did not boot cleanly",
               finished_at=_utcnow())
        return record

    except FileNotFoundError:
        update(status="failed", reason="packer not installed",
               error="packer not found on PATH", finished_at=_utcnow())
        return record
    except Exception as e:  # noqa: BLE001
        update(status="failed", reason=f"error: {str(e)[:120]}",
               error=str(e), finished_at=_utcnow())
        return record
    finally:
        with _procs_lock:
            _procs.pop(validation_id, None)
        # ALWAYS tear down the transient validation image, even on failure.
        if transient_img:
            log_append(f"\ndeleting transient validation image {transient_img}\n")
            try:
                delete_image(transient_img)
            except Exception as e:  # noqa: BLE001
                log_append(f"warning: could not delete {transient_img}: {e}\n")
