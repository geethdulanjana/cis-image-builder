#!/usr/bin/env python3
"""
build_runner.py — execute a Packer CIS image build on the builder CVM.

Responsibilities:
  1. Take a generated group_vars all.yml (string) for a build.
  2. Write it into the RHEL10-CIS pipeline's ansible group_vars path.
  3. Refresh temporary Tencent credentials from the CAM role metadata.
  4. Run `packer build`, streaming output to a per-build log file.
  5. Parse the resulting custom image id from Packer output.
  6. Report status back via a callback (used by the API to update the store).

This module is designed to run ON the builder CVM (it relies on the instance
metadata service for credentials and on packer/ansible being installed there).

It is import-safe on any platform: nothing runs at import time. On Windows (dev)
the run() will simply fail when packer/metadata are absent, which is expected —
use DRY_RUN for local testing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import urllib.request
from datetime import datetime, timezone

# Paths on the builder CVM. Overridable via env for flexibility/testing.
PIPELINE_DIR = os.environ.get("CIS_PIPELINE_DIR", "/opt/cis-pipeline")
GROUP_VARS_PATH = os.environ.get(
    "CIS_GROUP_VARS_PATH",
    os.path.join(PIPELINE_DIR, "ansible", "group_vars", "all.yml"),
)
PACKER_DIR = os.environ.get("CIS_PACKER_DIR", os.path.join(PIPELINE_DIR, "packer"))
LOG_DIR = os.environ.get("CIS_BUILD_LOG_DIR", os.path.join(PIPELINE_DIR, "logs"))
METADATA_BASE = "http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/"

IMG_ID = r"img-[0-9a-z]{6,}"
# A bare image id (used only for last-resort matching / validation).
IMAGE_ID_RE = re.compile(r"\b" + IMG_ID + r"\b")

# Lines that indicate a NEWLY CREATED image (not the source/base image).
# Tencent's Packer builder prints the created image id in its artifact output,
# e.g. "A disk image was created: img-xxxx" or "Image ID: img-xxxx". We match
# only these success contexts so a failed build (whose log still contains the
# base source_image_id) does NOT get a spurious image_id.
CREATED_IMAGE_RES = [
    re.compile(r"(?:disk )?image (?:was )?created[^\n]*?\b(" + IMG_ID + r")\b", re.I),
    re.compile(r"\bImage ID:\s*(" + IMG_ID + r")\b", re.I),
    re.compile(r"Creating image[^\n]*?\b(" + IMG_ID + r")\b", re.I),
    re.compile(r"Artifacts of successful build[^\0]*?\b(" + IMG_ID + r")\b", re.I),
]

# The base/source image must never be treated as a build output or deleted.
BASE_IMAGE_ID = os.environ.get("CIS_BASE_IMAGE_ID", "").strip()

# Registry of running builds so the API can cancel them:
#   { build_id: subprocess.Popen }
_procs = {}
_procs_lock = threading.Lock()


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def summarize_outcome(text: str, returncode, canceled: bool = False) -> str:
    """Produce a short human-readable outcome reason from packer/ansible output.

    Best-effort heuristics tuned for this pipeline (RHSM license, ansible task
    failures, common packer errors). Returns "" if nothing notable is found.
    """
    if canceled:
        return "canceled by user"
    t = text.lower()
    # License / subscription (the current known blocker).
    if "subscription-manager" in t or "not registered" in t or "this system is not registered" in t \
            or "no enabled repositories" in t or "rhsm" in t:
        return "failed: RHEL subscription/repos required (license)"
    # Ansible task failure — capture the failing task name if present.
    m = re.search(r"failed:\s*\[[^\]]+\][^\n]*", text, re.I)
    fatal = re.search(r"fatal:\s*\[[^\]]+\][^\n]*?\"msg\":\s*\"([^\"]+)\"", text, re.I)
    if fatal:
        return f"failed (ansible): {fatal.group(1)[:120]}"
    if "play_recap" in t or "play recap" in t:
        rc = re.search(r"failed=(\d+)", text)
        if rc and rc.group(1) != "0":
            return f"failed: ansible reported failed={rc.group(1)}"
    # Common packer/infra errors.
    if "no such file or directory" in t and "packer" in t:
        return "failed: packer path/config issue"
    if "quota" in t or "limitexceeded" in t:
        return "failed: Tencent quota/limit exceeded"
    if "authfailure" in t or "unauthorizedoperation" in t:
        return "failed: Tencent auth/permission (check CAM role)"
    if returncode == 0:
        return "succeeded"
    if returncode is not None and returncode != 0:
        return f"failed: build exited {returncode}"
    return ""


def refresh_credentials(env: dict) -> dict:
    """Fetch temp creds from the CAM role metadata and add them to env.

    Returns the mutated env. Raises on failure (caller decides how to handle).
    """
    role = urllib.request.urlopen(METADATA_BASE, timeout=5).read().decode().strip()
    creds_raw = urllib.request.urlopen(METADATA_BASE + role, timeout=5).read().decode()
    creds = json.loads(creds_raw)
    env["TENCENTCLOUD_SECRET_ID"] = creds["TmpSecretId"]
    env["TENCENTCLOUD_SECRET_KEY"] = creds["TmpSecretKey"]
    env["TENCENTCLOUD_SECURITY_TOKEN"] = creds["Token"]
    env["TENCENTCLOUD_TOKEN"] = creds["Token"]
    return env


def write_group_vars(all_yaml: str, path: str = GROUP_VARS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(all_yaml)
    return path


AUDIT_STAGING = os.path.join(PIPELINE_DIR, "audit-results", "latest")
AUDIT_STORE = os.environ.get(
    "CIS_AUDIT_DIR", os.path.join(PIPELINE_DIR, "audit-results"))


def collect_openscap_audit(build_id: str) -> dict:
    """Move the staged OpenSCAP results (audit-results/latest/) into a per-build
    dir and parse the score. Returns audit metadata for the build record.
    Best-effort — returns {available: False} if nothing was produced."""
    import shutil

    staging = AUDIT_STAGING
    if not os.path.isdir(staging):
        return {"available": False}
    dest = os.path.join(AUDIT_STORE, build_id)
    try:
        os.makedirs(AUDIT_STORE, exist_ok=True)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.move(staging, dest)
    except OSError as e:
        return {"available": False, "error": f"could not move audit results: {e}"}

    score = None
    score_file = os.path.join(dest, "score.txt")
    if os.path.exists(score_file):
        try:
            score = float(open(score_file, encoding="utf-8").read().strip())
        except (ValueError, OSError):
            score = None

    passed = failed = None
    results_xml = os.path.join(dest, "results.xml")
    if os.path.exists(results_xml):
        try:
            txt = open(results_xml, encoding="utf-8", errors="replace").read()
            passed = txt.count("<result>pass</result>")
            failed = txt.count("<result>fail</result>")
        except OSError:
            pass

    report_path = os.path.join(dest, "report.html")
    report_exists = os.path.exists(report_path)
    # Only mark 'available' when there's a real HTML report to serve. If OpenSCAP
    # couldn't run (e.g. SSG/scanner not installed, no repos), score.txt may be
    # 0 with no report — don't advertise a report link that 404s.
    return {
        "available": report_exists,
        "score": score,
        "passed": passed,
        "failed": failed,
        "report_path": report_path if report_exists else None,
        "results_dir": dest,
        "note": None if report_exists else "no OpenSCAP report produced (scanner/content missing?)",
    }


VALIDATE_STAGING = os.path.join(PIPELINE_DIR, "audit-results", "latest-validate")
# The validate template lives in its OWN subdir (packer/validate/) so Packer
# doesn't merge it with the main template (which would duplicate variables).
VALIDATE_DIR = os.environ.get(
    "CIS_VALIDATE_DIR", os.path.join(PACKER_DIR, "validate"))
# Toggle: set CIS_POSTBOOT_VALIDATE=0 to skip the post-boot verification.
POSTBOOT_VALIDATE = os.environ.get("CIS_POSTBOOT_VALIDATE", "1") == "1"

# Main pkrvars file (network/region values) to reuse for the validate build.
MAIN_PKRVARS = os.environ.get(
    "CIS_MAIN_PKRVARS", os.path.join(PACKER_DIR, "variables.auto.pkrvars.hcl"))


def _read_main_pkrvars():
    """Read simple `key = "value"` pairs from the main pkrvars file so the
    validate build can reuse network settings. Lightweight line parser (the file
    is flat HCL assignments), tolerant of comments/blank lines."""
    out = {}
    if not os.path.exists(MAIN_PKRVARS):
        return out
    try:
        with open(MAIN_PKRVARS, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"', line)
                if m:
                    out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


def run_postboot_validation(build_id, image_id, log_append, env):
    """Boot a throwaway CVM from the hardened image, reboot it, run OpenSCAP,
    and return the verified audit metadata. Deletes the transient validation
    image it produces. Best-effort — returns {available: False} on any problem.
    """
    import shutil

    validate_dir = os.path.abspath(VALIDATE_DIR)
    if not os.path.isdir(validate_dir):
        log_append(f"post-boot validation skipped: dir not found ({validate_dir})\n")
        return {"available": False, "error": "validate dir missing"}

    # Clear any stale staging dir.
    if os.path.isdir(VALIDATE_STAGING):
        shutil.rmtree(VALIDATE_STAGING, ignore_errors=True)

    # Run packer against the validate SUBDIR, but with cwd at the pipeline root
    # so the file-download destination (audit-results/latest-validate) and the
    # script's ../../ path resolve correctly.
    pipeline_root = os.path.abspath(PIPELINE_DIR)

    # The validate template needs the same network vars as the main build, but
    # Packer only auto-loads *.auto.pkrvars.hcl from the template/cwd dir — which
    # isn't the validate subdir. Pull vpc/subnet/sg/region/zone/instance_type
    # from the main pkrvars and pass them explicitly.
    net_vars = _read_main_pkrvars()
    cmd = ["packer", "build", "-var", f"hardened_image_id={image_id}"]
    for k in ("region", "zone", "instance_type", "vpc_id", "subnet_id", "security_group_id"):
        if net_vars.get(k):
            cmd += ["-var", f"{k}={net_vars[k]}"]
    cmd.append(validate_dir)
    log_append(f"\n===== POST-BOOT VALIDATION: {' '.join(cmd)} (cwd={pipeline_root}) =====\n")
    captured = []
    try:
        proc = subprocess.Popen(
            cmd, cwd=pipeline_root,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log_append(line)
            captured.append(line)
        proc.wait()
    except FileNotFoundError:
        return {"available": False, "error": "packer not found"}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)}

    full = "".join(captured)
    # The validation template creates a transient image — delete it.
    validate_img = None
    m = IMAGE_ID_RE.findall(full)
    if m:
        # the last img- that is NOT the hardened image is the transient one
        for cand in reversed(m):
            if cand != image_id:
                validate_img = cand
                break
    if validate_img:
        log_append(f"deleting transient validation image {validate_img}\n")
        delete_image(validate_img)

    # Move staged results into a per-build 'verified' dir and parse the score.
    if not os.path.isdir(VALIDATE_STAGING):
        return {"available": False, "error": "no post-boot results produced"}
    dest = os.path.join(AUDIT_STORE, f"{build_id}-verified")
    try:
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.move(VALIDATE_STAGING, dest)
    except OSError as e:
        return {"available": False, "error": f"could not move results: {e}"}

    score = None
    sf = os.path.join(dest, "score.txt")
    if os.path.exists(sf):
        try:
            score = float(open(sf, encoding="utf-8").read().strip())
        except (ValueError, OSError):
            pass
    passed = failed = None
    rx = os.path.join(dest, "results.xml")
    if os.path.exists(rx):
        try:
            txt = open(rx, encoding="utf-8", errors="replace").read()
            passed = txt.count("<result>pass</result>")
            failed = txt.count("<result>fail</result>")
        except OSError:
            pass
    report_path = os.path.join(dest, "report.html")
    report_exists = os.path.exists(report_path)
    return {
        "available": report_exists, "verified": True, "score": score,
        "passed": passed, "failed": failed,
        "report_path": report_path if report_exists else None,
        "results_dir": dest,
        "note": None if report_exists else "no OpenSCAP report produced (scanner/content missing?)",
    }


def parse_image_id(text):
    """Return the id of the image Packer CREATED, or None.

    Only matches success-context lines (see CREATED_IMAGE_RES), and never
    returns the configured base image id. This prevents a failed build — whose
    log still contains the source_image_id — from being tagged with (and later
    offering to delete) the base image.
    """
    candidates = []
    for rx in CREATED_IMAGE_RES:
        for m in rx.finditer(text):
            candidates.append(m.group(1))
    # Drop the base image id if it slipped into a matched context.
    if BASE_IMAGE_ID:
        candidates = [c for c in candidates if c != BASE_IMAGE_ID]
    return candidates[-1] if candidates else None


def run_build(build_id: str, all_yaml: str, on_update=None, dry_run: bool = False,
              base_image_id: str = "", distro: str = "") -> dict:
    """Run one build. Returns the final build record.

    base_image_id: if provided, passed to Packer as `-var source_image_id=...`
      so the UI can select the base image per build. Must be a valid img- id.
    distro: recorded on the build for display (the actual role selection is
      driven by cis_distro inside the generated all.yml).

    on_update(record): optional callback invoked as status changes so the API
    can persist progress.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(LOG_DIR, f"build-{build_id}-{ts}.log")

    record = {
        "build_id": build_id,
        "status": "preparing",
        "log_path": log_path,
        "image_id": None,
        "started_at": _utcnow(),
        "finished_at": None,
        "error": None,
        "reason": None,
        "distro": distro or None,
        "base_image_id": base_image_id or None,
        "audit": None,
        "audit_verified": None,
    }

    # Validate base image id early (if provided).
    if base_image_id and not re.fullmatch(r"img-[0-9a-z]{6,}", base_image_id):
        def _fail_update(**kw):
            record.update(kw)
            if on_update:
                on_update(dict(record))
        _fail_update(status="failed",
                     error=f"invalid base image id: {base_image_id!r}",
                     reason="failed: invalid base image id", finished_at=_utcnow())
        return record

    def update(**kw):
        record.update(kw)
        if on_update:
            on_update(dict(record))

    update(status="preparing")

    try:
        write_group_vars(all_yaml)
    except OSError as e:
        update(status="failed", error=f"could not write group_vars: {e}",
               finished_at=_utcnow())
        return record

    if dry_run:
        # Local/dev path: don't touch metadata or packer. Simulate success.
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("DRY RUN — group_vars written, packer not executed.\n")
            f.write(f"distro: {distro or '(from all.yml)'}\n")
            f.write(f"base_image_id: {base_image_id or '(template default)'}\n\n")
            f.write(all_yaml)
        update(status="succeeded", image_id="img-dryrun00",
               reason="succeeded (dry-run)", finished_at=_utcnow())
        return record

    env = dict(os.environ)
    try:
        refresh_credentials(env)
    except Exception as e:  # noqa: BLE001 — surface any metadata failure
        update(status="failed", error=f"credential refresh failed: {e}",
               finished_at=_utcnow())
        return record

    # Validate the packer directory exists before invoking packer, so we fail
    # with a clear message instead of packer's cryptic "stat packer/: no such
    # file or directory". PACKER_DIR is overridable via CIS_PACKER_DIR.
    packer_dir = os.path.abspath(PACKER_DIR)
    if not os.path.isdir(packer_dir):
        update(
            status="failed",
            error=(
                f"packer dir not found: {packer_dir}. "
                f"Set CIS_PACKER_DIR (and CIS_PIPELINE_DIR) to your real paths. "
                f"CIS_PIPELINE_DIR={PIPELINE_DIR}"
            ),
            finished_at=_utcnow(),
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(record["error"] + "\n")
        return record

    # PACKER_LOG=1 emits very verbose internal [INFO]/[DEBUG] lines. Keep it OFF
    # by default for readable logs; set CIS_PACKER_DEBUG=1 to turn it back on.
    if os.environ.get("CIS_PACKER_DEBUG", "0") == "1":
        env["PACKER_LOG"] = "1"
    else:
        env.pop("PACKER_LOG", None)
    update(status="running")

    # Build the packer command. If a base image id was supplied from the UI,
    # override the template's source_image_id via -var so the user can pick the
    # base image per build.
    packer_cmd = ["packer", "build"]
    if base_image_id:
        packer_cmd += ["-var", f"source_image_id={base_image_id}"]
    packer_cmd.append(packer_dir)

    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(f"Running: {' '.join(packer_cmd)}\n")
            logf.write(f"CWD: {os.path.dirname(packer_dir)}\n")
            logf.write(f"distro: {distro or '(from all.yml)'}  "
                       f"base_image_id: {base_image_id or '(template default)'}\n\n")
            logf.flush()
            proc = subprocess.Popen(
                packer_cmd,
                cwd=os.path.dirname(packer_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            # Register so the API can cancel this build.
            with _procs_lock:
                _procs[build_id] = proc
            captured = []
            assert proc.stdout is not None
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
                captured.append(line)
            proc.wait()

        full = "".join(captured)
        canceled = record.get("status") == "canceling"
        # Only a SUCCESSFUL build produces an image. For failed/canceled builds
        # force image_id=None so we never tag them with (and later offer to
        # delete) the base/source image whose id appears in the log.
        image_id = parse_image_id(full) if (proc.returncode == 0 and not canceled) else None

        if canceled:
            update(status="canceled", image_id=None,
                   reason=summarize_outcome(full, proc.returncode, canceled=True),
                   error="canceled by user", finished_at=_utcnow())
        elif proc.returncode == 0:
            audit = collect_openscap_audit(build_id)  # preliminary (pre-reboot)
            update(status="succeeded", image_id=image_id,
                   reason=summarize_outcome(full, 0), audit=audit)

            # Authoritative post-boot verification: boot the image, reboot, scan.
            if POSTBOOT_VALIDATE and image_id:
                def _log_append(text):
                    try:
                        with open(log_path, "a", encoding="utf-8") as lf:
                            lf.write(text)
                    except OSError:
                        pass
                update(status="verifying")
                verified = run_postboot_validation(build_id, image_id, _log_append, env)
                update(status="succeeded", audit_verified=verified,
                       finished_at=_utcnow())
            else:
                update(finished_at=_utcnow())
        else:
            update(status="failed", image_id=None,
                   reason=summarize_outcome(full, proc.returncode),
                   error=f"packer exited {proc.returncode}", finished_at=_utcnow())
    except FileNotFoundError:
        update(status="failed", error="packer not found on PATH",
               reason="failed: packer not installed", finished_at=_utcnow())
    except Exception as e:  # noqa: BLE001
        update(status="failed", error=str(e),
               reason=f"failed: {str(e)[:120]}", finished_at=_utcnow())
    finally:
        with _procs_lock:
            _procs.pop(build_id, None)

    return record


def cancel_build(build_id: str, mark_status_cb=None) -> bool:
    """Terminate a running build's packer process. Returns True if a process was
    found and signaled. mark_status_cb(status) lets the caller flag the record
    as 'canceling' so run_build reports it as 'canceled' when the process exits.
    """
    with _procs_lock:
        proc = _procs.get(build_id)
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


def is_running(build_id: str) -> bool:
    with _procs_lock:
        return build_id in _procs


def delete_image(image_id: str) -> dict:
    """Delete a Tencent custom image by id using tccli with CAM-role creds.

    Returns {"ok": bool, "detail": str}. Runs on the builder CVM (needs tccli
    and cvm:DeleteImages permission on the CAM role).
    """
    if not re.fullmatch(r"img-[0-9a-z]{6,}", image_id or ""):
        return {"ok": False, "detail": f"invalid image id: {image_id!r}"}
    # Never delete the configured base/source image.
    if BASE_IMAGE_ID and image_id == BASE_IMAGE_ID:
        return {"ok": False, "detail": f"refusing to delete the base image {image_id}"}

    env = dict(os.environ)
    try:
        refresh_credentials(env)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"credential refresh failed: {e}"}

    # tccli reads creds from env; pass token explicitly for the temporary role.
    env["TENCENTCLOUD_REGION"] = os.environ.get("TENCENTCLOUD_REGION", "ap-singapore")
    cmd = [
        "tccli", "cvm", "DeleteImages",
        "--ImageIds", json.dumps([image_id]),
        "--region", env["TENCENTCLOUD_REGION"],
    ]
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return {"ok": False, "detail": "tccli not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "tccli DeleteImages timed out"}

    if out.returncode == 0:
        return {"ok": True, "detail": out.stdout.strip() or "deleted"}
    return {"ok": False, "detail": (out.stderr or out.stdout).strip()[:300]}


if __name__ == "__main__":
    # Simple manual test in dry-run mode.
    import sys
    sample = "---\nrhel10cis_section1: true\n"
    rec = run_build("test123", sample, on_update=lambda r: print(r["status"]),
                    dry_run="--dry-run" in sys.argv)
    print(rec)
