#!/usr/bin/env python3
"""
app.py — FastAPI backend for the CIS Image Builder.

Endpoints:
  GET  /api/catalog?distro=    -> the control catalog for a distro (rhel10/rhel9)
  GET  /api/distros            -> supported distros + availability
  GET  /api/presets            -> available presets
  POST /api/preview            -> render group_vars all.yml for a selection/preset
  POST /api/builds             -> submit a build (async); returns build_id
  GET  /api/builds             -> list build history
  GET  /api/builds/{id}        -> one build record
  GET  /api/builds/{id}/log    -> tail of the build log
  POST /api/builds/{id}/cancel -> cancel a running build
  DELETE /api/images/{id}      -> delete a Tencent custom image
  GET/POST/DELETE /api/selections -> manage saved custom selections
  GET  /api/status             -> build-running flag + dry-run/auth state
  GET  /                       -> serves the UI (frontend/index.html)

Build state is persisted to a JSON file (STORE_PATH) for the MVP. Builds run in
a background thread via build_runner. On the builder CVM this executes Packer;
set DRY_RUN=1 for local development (no packer/metadata needed).
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone

from typing import Dict, List, Optional

import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import build_runner
import validation_runner
import vars_generator

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
CATALOG_DIR = os.path.join(ROOT, "catalog")
PRESETS_PATH = os.path.join(HERE, "presets.json")
DEFAULT_DISTRO = "rhel10"
SUPPORTED_DISTROS = ["rhel10", "rhel9"]

# Matches an Ansible TASK header line that carries a CIS control id, e.g.:
#   TASK [.../RHEL10-CIS : 3.3.1.1 | PATCH | Ensure net.ipv4.ip_forward ...]
_TASK_LINE_RE = re.compile(r"TASK \[[^\]]*?\b([0-9]+(?:\.[0-9]+)+)\s*\|")

# Strip ANSI color codes (Packer/ansible colorize output).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Ansible per-task result tokens — searched ANYWHERE in the line because Packer
# prefixes each line (e.g. "==> tencentcloud-cvm.rhel10_cis: ok: [default]").
_RESULT_RE = re.compile(r"\b(ok|changed|skipping|skipped|failed|fatal):\s*\[")
# The authoritative end-of-run tally line, e.g.:
#   default : ok=171 changed=76 unreachable=0 failed=0 skipped=612 ...
_RECAP_RE = re.compile(
    r"ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+skipped=(\d+)"
)


def parse_progress_from_log(path):
    """Parse ok/changed/skipped/failed from an Ansible-through-Packer log.

    Robust to ANSI color codes and Packer line prefixes. If a PLAY RECAP line is
    present (end of run), its authoritative totals are used instead of the
    line-by-line tally."""
    counts = {"ok": 0, "changed": 0, "skipped": 0, "failed": 0, "current": None,
              "source": "live"}
    recap = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = _ANSI_RE.sub("", raw)
                rc = _RECAP_RE.search(line)
                if rc:
                    recap = rc  # keep the last recap seen
                mt = _TASK_LINE_RE.search(line)
                if mt:
                    counts["current"] = mt.group(1)
                    continue
                mr = _RESULT_RE.search(line)
                if mr:
                    tok = mr.group(1)
                    if tok == "ok":
                        counts["ok"] += 1
                    elif tok == "changed":
                        counts["changed"] += 1
                    elif tok in ("skipping", "skipped"):
                        counts["skipped"] += 1
                    elif tok in ("failed", "fatal"):
                        counts["failed"] += 1
    except OSError:
        return counts

    if recap:
        counts = {
            "ok": int(recap.group(1)),
            "changed": int(recap.group(2)),
            "unreachable": int(recap.group(3)),
            "failed": int(recap.group(4)),
            "skipped": int(recap.group(5)),
            "current": counts["current"],
            "source": "recap",
        }
    return counts


# Status precedence when a control has multiple tasks (worst wins).
_STATUS_RANK = {"skipped": 0, "ok": 1, "changed": 2, "failed": 3}


def parse_results_from_log(path):
    """Parse the log into a PER-CONTROL outcome.

    Each CIS control can expand into several Ansible tasks; we attribute each
    task result to the control id from the most recent TASK header, then roll up
    to one status per control (worst wins: failed > changed > ok > skipped).

    Returns { "by_control": { id: status }, "groups": { ok:[ids], changed:[...],
    skipped:[...], failed:[...] }, "untagged": {counts} }.
    'untagged' holds results that occurred outside a control-id'd task (e.g.
    gather-facts, prep tasks)."""
    by_control = {}
    current = None
    untagged = {"ok": 0, "changed": 0, "skipped": 0, "failed": 0}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = _ANSI_RE.sub("", raw)
                mt = _TASK_LINE_RE.search(line)
                if mt:
                    current = mt.group(1)
                    continue
                # A non-control TASK header resets 'current' so unrelated results
                # (facts, prep) aren't misattributed to the last control.
                if "TASK [" in line and not mt:
                    current = None
                mr = _RESULT_RE.search(line)
                if not mr:
                    continue
                tok = mr.group(1)
                status = ("failed" if tok in ("failed", "fatal")
                          else "skipped" if tok in ("skipping", "skipped")
                          else tok)  # ok / changed
                if current is None:
                    untagged[status] = untagged.get(status, 0) + 1
                    continue
                prev = by_control.get(current)
                if prev is None or _STATUS_RANK[status] > _STATUS_RANK[prev]:
                    by_control[current] = status
    except OSError:
        pass

    groups = {"ok": [], "changed": [], "skipped": [], "failed": []}
    for cid, st in by_control.items():
        groups[st].append(cid)
    for g in groups.values():
        g.sort(key=lambda c: [int(x) for x in c.split(".")])
    return {"by_control": by_control, "groups": groups, "untagged": untagged}


def _catalog_path(distro: str) -> str:
    if distro not in SUPPORTED_DISTROS:
        raise HTTPException(status_code=400, detail=f"unsupported distro '{distro}'")
    return os.path.join(CATALOG_DIR, f"catalog-{distro}.json")


def _details_path(distro: str) -> str:
    if distro not in SUPPORTED_DISTROS:
        raise HTTPException(status_code=400, detail=f"unsupported distro '{distro}'")
    return os.path.join(CATALOG_DIR, f"control-details-{distro}.json")


# Cache details files in memory (they're ~400KB and immutable per deploy).
_details_cache = {}


def _load_details(distro: str) -> dict:
    if distro not in _details_cache:
        path = _details_path(distro)
        if not os.path.exists(path):
            _details_cache[distro] = {"controls": {}}
        else:
            with open(path, encoding="utf-8-sig") as f:
                _details_cache[distro] = json.load(f)
    return _details_cache[distro]
FRONTEND_DIR = os.path.join(ROOT, "frontend")
STORE_PATH = os.environ.get("CIS_STORE_PATH", os.path.join(HERE, "builds_store.json"))
VALIDATIONS_PATH = os.environ.get(
    "CIS_VALIDATIONS_PATH", os.path.join(HERE, "validations_store.json"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

# Shared access token. If CIS_AUTH_TOKEN is set, all /api/* requests must send
# it as `X-Auth-Token: <token>` (or `Authorization: Bearer <token>`). If unset,
# auth is DISABLED (dev convenience) and a warning is logged at startup.
AUTH_TOKEN = os.environ.get("CIS_AUTH_TOKEN", "").strip()

app = FastAPI(title="CIS Image Builder", version="0.2.0")

_store_lock = threading.Lock()

# Build lock: only one build may run at a time. Guards the shared group_vars
# file and the single Packer workspace on the builder.
_build_lock = threading.Lock()
_active_build = {"build_id": None}


def require_auth(
    x_auth_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """FastAPI dependency enforcing the shared token when one is configured."""
    if not AUTH_TOKEN:
        return  # auth disabled
    supplied = x_auth_token
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if supplied != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="missing or invalid auth token")


# ── Persistence (simple JSON file store) ──────────────────────────────────────
def _load_store() -> dict:
    if not os.path.exists(STORE_PATH):
        return {"builds": {}}
    with open(STORE_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def _save_store(store: dict):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, STORE_PATH)


def _upsert_build(record: dict):
    with _store_lock:
        store = _load_store()
        store["builds"][record["build_id"]] = {
            **store["builds"].get(record["build_id"], {}),
            **record,
        }
        _save_store(store)


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


# ── Validations store (separate JSON file, same pattern as builds) ────────────
_val_lock = threading.Lock()


def _load_validations() -> dict:
    if not os.path.exists(VALIDATIONS_PATH):
        return {"validations": {}}
    with open(VALIDATIONS_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def _save_validations(data: dict):
    tmp = VALIDATIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, VALIDATIONS_PATH)


def _upsert_validation(record: dict):
    with _val_lock:
        data = _load_validations()
        data["validations"][record["validation_id"]] = {
            **data["validations"].get(record["validation_id"], {}),
            **record,
        }
        _save_validations(data)


# ── Request models ────────────────────────────────────────────────────────────
class Selection(BaseModel):
    sections: Optional[Dict[str, bool]] = None
    levels: Optional[Dict[str, bool]] = None
    disabled_controls: Optional[List[str]] = None
    enabled_controls: Optional[List[str]] = None
    tke_safe: bool = True
    cloud_image_exceptions: bool = True
    run_audit: bool = False


class BuildRequest(BaseModel):
    preset: Optional[str] = None
    selection: Optional[Selection] = None
    label: Optional[str] = None
    distro: Optional[str] = None          # "rhel10" | "rhel9"
    base_image_id: Optional[str] = None   # img-xxxx; overrides template default


class ValidationRequest(BaseModel):
    image_id: str                         # the hardened image to validate (img-xxxx)
    label: Optional[str] = None
    distro: Optional[str] = None


def _resolve_distro(req: BuildRequest) -> str:
    d = (req.distro or DEFAULT_DISTRO)
    if d not in SUPPORTED_DISTROS:
        raise HTTPException(status_code=400, detail=f"unsupported distro '{d}'")
    return d


def _render_yaml(req: BuildRequest, distro: str) -> str:
    sel = req.selection.model_dump() if req.selection else None
    try:
        return vars_generator.generate(
            selection=sel,
            preset=req.preset,
            distro=distro,
            presets_path=PRESETS_PATH,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


def _resolve_selection(req: BuildRequest, distro: str) -> dict:
    """Return the effective selection dict (preset expanded or raw selection)."""
    if req.selection:
        return req.selection.model_dump()
    # Expand the preset into a selection shape.
    with open(PRESETS_PATH, encoding="utf-8-sig") as f:
        presets = json.load(f)["presets"]
    p = presets.get(req.preset or "", {})
    return {
        "sections": p.get("sections", {}),
        "levels": p.get("levels", {}),
        "disabled_controls": p.get("disabled_controls", []),
        "enabled_controls": p.get("enabled_controls", []),
        "tke_safe": p.get("tke_safe", True),
        "cloud_image_exceptions": p.get("cloud_image_exceptions", True),
    }


def _build_manifest(selection: dict, distro: str) -> dict:
    """Compute the applied-controls manifest: for this selection, which controls
    are effectively ENABLED, each with a snapshot of its Ansible detail. This is
    the audit record of exactly what the image applies."""
    with open(_catalog_path(distro), encoding="utf-8-sig") as f:
        catalog = json.load(f)
    details = _load_details(distro).get("controls", {})

    sections = selection.get("sections") or {str(i): True for i in range(1, 8)}
    disabled = set(selection.get("disabled_controls") or [])
    enabled_forced = set(selection.get("enabled_controls") or [])

    applied = []
    for c in catalog["controls"]:
        sec_on = sections.get(c["section"], True) is not False
        is_on = (sec_on and c["id"] not in disabled) or c["id"] in enabled_forced
        if not is_on:
            continue
        d = details.get(c["id"], {})
        applied.append({
            "id": c["id"],
            "title": c.get("title", ""),
            "section": c["section"],
            "tke_risk": c.get("tke_risk", "safe"),
            "modules": d.get("modules", []),
            "commands": d.get("commands", []),
            "files": d.get("files", []),
            # tasks_yaml is large; include it so the manifest is self-contained
            # and auditable offline. Can be trimmed if size becomes an issue.
            "tasks_yaml": d.get("tasks_yaml", ""),
        })
    return {
        "distro": distro,
        "benchmark": catalog.get("benchmark"),
        "generated_at": _utcnow(),
        "applied_count": len(applied),
        "controls": applied,
    }


# ── API endpoints ─────────────────────────────────────────────────────────────
@app.get("/api/catalog", dependencies=[Depends(require_auth)])
def get_catalog(distro: str = DEFAULT_DISTRO):
    with open(_catalog_path(distro), encoding="utf-8-sig") as f:
        return JSONResponse(json.load(f))


@app.get("/api/distros", dependencies=[Depends(require_auth)])
def get_distros():
    """List supported distros and whether each catalog is present."""
    out = []
    for d in SUPPORTED_DISTROS:
        out.append({"distro": d, "available": os.path.exists(_catalog_path(d))})
    return {"distros": out, "default": DEFAULT_DISTRO}


@app.get("/api/control/{control_id}", dependencies=[Depends(require_auth)])
def get_control_detail(control_id: str, distro: str = DEFAULT_DISTRO):
    """Return the Ansible task detail for one control: the raw task YAML that
    implements it, any literal shell commands, modules touched, and source
    file(s). This is read from the role, so it reflects exactly what runs."""
    details = _load_details(distro)
    d = details.get("controls", {}).get(control_id)
    if not d:
        raise HTTPException(status_code=404,
                            detail=f"no detail for control '{control_id}' in {distro}")
    return d


@app.get("/api/presets", dependencies=[Depends(require_auth)])
def get_presets():
    with open(PRESETS_PATH, encoding="utf-8-sig") as f:
        return JSONResponse(json.load(f))


@app.get("/api/status", dependencies=[Depends(require_auth)])
def status():
    """Lightweight status: is a build running, and which one."""
    return {
        "build_running": _active_build["build_id"] is not None,
        "active_build_id": _active_build["build_id"],
        "dry_run": DRY_RUN,
        "auth_enabled": bool(AUTH_TOKEN),
    }


@app.post("/api/preview", response_class=PlainTextResponse,
          dependencies=[Depends(require_auth)])
def preview(req: BuildRequest):
    if not req.preset and not req.selection:
        raise HTTPException(status_code=400, detail="Provide preset or selection")
    return _render_yaml(req, _resolve_distro(req))


@app.post("/api/builds", dependencies=[Depends(require_auth)])
def submit_build(req: BuildRequest):
    if not req.preset and not req.selection:
        raise HTTPException(status_code=400, detail="Provide preset or selection")

    distro = _resolve_distro(req)
    base_image_id = (req.base_image_id or "").strip()
    if base_image_id and not re.fullmatch(r"img-[0-9a-z]{6,}", base_image_id):
        raise HTTPException(status_code=400, detail=f"invalid base image id: {base_image_id}")

    # Render (and validate) BEFORE taking the lock, so a bad selection returns
    # 400 without holding the build lock.
    all_yaml = _render_yaml(req, distro)

    # One build at a time. Acquire the lock non-blocking; reject if busy.
    if not _build_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(
                f"a build is already running (build_id="
                f"{_active_build['build_id']}). Wait for it to finish."
            ),
        )

    build_id = uuid.uuid4().hex[:12]
    _active_build["build_id"] = build_id

    # Audit manifest: snapshot exactly which controls are applied + their
    # Ansible detail, so each image has a reproducible record of what went in.
    try:
        manifest = _build_manifest(_resolve_selection(req, distro), distro)
    except Exception:  # noqa: BLE001 — manifest is best-effort, never block a build
        manifest = {"distro": distro, "applied_count": 0, "controls": [], "error": "manifest build failed"}

    record = {
        "build_id": build_id,
        "label": req.label or req.preset or "custom",
        "status": "queued",
        "image_id": None,
        "distro": distro,
        "base_image_id": base_image_id or None,
        "created_at": _utcnow(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "vars_yaml": all_yaml,
        "manifest": manifest,
        "applied_count": manifest.get("applied_count", 0),
    }
    _upsert_build(record)

    def _worker():
        try:
            build_runner.run_build(
                build_id,
                all_yaml,
                on_update=lambda r: _upsert_build(r),
                dry_run=DRY_RUN,
                base_image_id=base_image_id,
                distro=distro,
            )
        finally:
            # Always release the lock, even if the build raised.
            _active_build["build_id"] = None
            _build_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return {"build_id": build_id, "status": "queued", "distro": distro, "dry_run": DRY_RUN}


@app.get("/api/builds", dependencies=[Depends(require_auth)])
def list_builds():
    store = _load_store()
    builds = sorted(
        store["builds"].values(),
        key=lambda b: b.get("created_at", ""),
        reverse=True,
    )
    # Keep the list light: drop the big YAML + manifest (fetched per build).
    slim = []
    for b in builds:
        b = {k: v for k, v in b.items() if k not in ("vars_yaml", "manifest")}
        slim.append(b)
    return {"builds": slim}


@app.get("/api/builds/{build_id}", dependencies=[Depends(require_auth)])
def get_build(build_id: str):
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    # Drop the heavy manifest here too; fetch it via the manifest endpoint.
    return {k: v for k, v in b.items() if k != "manifest"}


@app.get("/api/builds/{build_id}/manifest", dependencies=[Depends(require_auth)])
def get_build_manifest(build_id: str):
    """The audit manifest: the applied controls + their Ansible detail for this
    build. This is the reproducible record of what the image hardened."""
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    m = b.get("manifest")
    if not m:
        raise HTTPException(status_code=404, detail="no manifest for this build")
    return m


@app.get("/api/builds/{build_id}/manifest/download", dependencies=[Depends(require_auth)])
def download_build_manifest(build_id: str):
    """Same manifest as a downloadable JSON attachment for records/compliance."""
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b or not b.get("manifest"):
        raise HTTPException(status_code=404, detail="no manifest for this build")
    payload = {
        "build_id": build_id,
        "label": b.get("label"),
        "distro": b.get("distro"),
        "base_image_id": b.get("base_image_id"),
        "image_id": b.get("image_id"),
        "status": b.get("status"),
        "created_at": b.get("created_at"),
        "manifest": b["manifest"],
    }
    headers = {"Content-Disposition": f'attachment; filename="manifest-{build_id}.json"'}
    return JSONResponse(payload, headers=headers)


@app.get("/api/builds/{build_id}/log", response_class=PlainTextResponse,
         dependencies=[Depends(require_auth)])
def get_build_log(build_id: str, tail: int = 200):
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    log_path = b.get("log_path")
    if not log_path or not os.path.exists(log_path):
        return "(no log yet)"
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-tail:])


@app.get("/api/builds/{build_id}/log/download")
def download_build_log(build_id: str, token: str = Query(default="")):
    """Download the FULL Packer build log as a file attachment.

    Uses a `token` query param (not X-Auth-Token) so a plain browser download
    link works — an anchor can't set custom headers."""
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="missing or invalid auth token")
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    log_path = b.get("log_path")
    if not log_path or not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="no log for this build")
    return FileResponse(log_path, media_type="text/plain",
                        filename=f"build-{build_id}.log")


@app.get("/api/builds/{build_id}/audit", dependencies=[Depends(require_auth)])
def get_build_audit(build_id: str):
    """OpenSCAP audit summary for a build (score, pass/fail counts)."""
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    a = b.get("audit")
    if not a or not a.get("available"):
        return {"available": False}
    return a


@app.get("/api/builds/{build_id}/audit/report")
def get_build_audit_report(build_id: str, token: str = Query(default=""),
                           kind: str = "preliminary"):
    """Serve an OpenSCAP HTML report for a build.

    kind = "preliminary" (pre-reboot, in-build) or "verified" (post-boot)."""
    if AUTH_TOKEN and token and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid auth token")
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    a = (b.get("audit_verified") if kind == "verified" else b.get("audit")) or {}
    rp = a.get("report_path")
    if not rp or not os.path.exists(rp):
        raise HTTPException(status_code=404, detail=f"no {kind} report for this build")
    return FileResponse(rp, media_type="text/html",
                        filename=f"openscap-{kind}-{build_id}.html")


@app.get("/api/builds/{build_id}/progress", dependencies=[Depends(require_auth)])
def get_build_progress(build_id: str):
    """Pass/fail/skip tally parsed from the build log (for finished builds or a
    one-shot check without opening the stream)."""
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    lp = b.get("log_path")
    if lp and os.path.exists(lp):
        return parse_progress_from_log(lp)
    return {"ok": 0, "changed": 0, "skipped": 0, "failed": 0, "current": None, "source": "none"}


@app.get("/api/builds/{build_id}/results", dependencies=[Depends(require_auth)])
def get_build_results(build_id: str):
    """Per-control outcome for a build: which controls actually ended up
    ok/changed/skipped/failed, parsed from the real Ansible log and enriched
    with control titles. This is 'what actually happened', vs the manifest's
    'what was selected'."""
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    lp = b.get("log_path")
    if not lp or not os.path.exists(lp):
        return {"groups": {"ok": [], "changed": [], "skipped": [], "failed": []},
                "untagged": {}, "ready": False}

    parsed = parse_results_from_log(lp)

    # Enrich with titles from the build's distro catalog.
    titles = {}
    try:
        with open(_catalog_path(b.get("distro") or DEFAULT_DISTRO), encoding="utf-8-sig") as f:
            for c in json.load(f)["controls"]:
                titles[c["id"]] = c.get("title", "")
    except Exception:  # noqa: BLE001
        pass

    def enrich(ids):
        return [{"id": cid, "title": titles.get(cid, "")} for cid in ids]

    g = parsed["groups"]
    return {
        "groups": {k: enrich(v) for k, v in g.items()},
        "counts": {k: len(v) for k, v in g.items()},
        "untagged": parsed["untagged"],
        "ready": True,
    }


@app.get("/api/builds/{build_id}/results.csv")
def download_build_results_csv(build_id: str, token: str = Query(default="")):
    """Per-control outcome as a CSV compliance report.

    Token accepted as a query param so the browser can download directly
    (the auth header can't be set on a plain anchor click; the UI still fetches
    with the header, but this keeps direct links working too)."""
    if AUTH_TOKEN and token and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid auth token")

    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    lp = b.get("log_path")
    if not lp or not os.path.exists(lp):
        raise HTTPException(status_code=404, detail="no log for this build")

    parsed = parse_results_from_log(lp)
    distro = b.get("distro") or DEFAULT_DISTRO
    titles, risks = {}, {}
    try:
        with open(_catalog_path(distro), encoding="utf-8-sig") as f:
            for c in json.load(f)["controls"]:
                titles[c["id"]] = c.get("title", "")
                risks[c["id"]] = c.get("tke_risk", "safe")
    except Exception:  # noqa: BLE001
        pass

    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["build_id", "distro", "image_id", "status", "generated_at"])
    w.writerow([build_id, distro, b.get("image_id") or "", b.get("status") or "", _utcnow()])
    w.writerow([])
    w.writerow(["control_id", "title", "outcome", "tke_risk"])
    # Sorted by control id, outcome as a column.
    for cid, outcome in sorted(parsed["by_control"].items(),
                               key=lambda kv: [int(x) for x in kv[0].split(".")]):
        w.writerow([cid, titles.get(cid, ""), outcome, risks.get(cid, "safe")])

    headers = {"Content-Disposition": f'attachment; filename="results-{build_id}.csv"'}
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers=headers)


@app.get("/api/builds/{build_id}/stream")
def stream_build_log(build_id: str, token: str = Query(default="")):
    """Server-Sent Events stream of the build log in real time.

    EventSource can't send custom headers, so the auth token is passed as the
    `token` query param here (instead of X-Auth-Token). Emits `data:` lines as
    the log grows and a final `event: done` when the build reaches a terminal
    state, then closes.
    """
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="missing or invalid auth token")

    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")

    terminal = {"succeeded", "failed", "canceled"}

    # On connect, start near the END of any existing log (last ~64KB) rather
    # than replaying the whole file — a long build can already have megabytes
    # of output, and dumping it all at once can overwhelm the browser.
    TAIL_BYTES = 64 * 1024

    def _event_gen():
        # Seek to the tail of the current file so we only stream new-ish output.
        pos = 0
        lp = b.get("log_path")
        if lp and os.path.exists(lp):
            size = os.path.getsize(lp)
            pos = max(0, size - TAIL_BYTES)
        last_status = None
        idle = 0
        while True:
            rec = _load_store()["builds"].get(build_id, {})
            status = rec.get("status")
            log_path = rec.get("log_path")
            if log_path and os.path.exists(log_path):
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                if chunk:
                    # SSE: each line prefixed with 'data: '
                    for line in chunk.splitlines():
                        yield f"data: {line}\n\n"
                    idle = 0
                else:
                    idle += 1
            else:
                idle += 1

            if status != last_status:
                yield f"event: status\ndata: {status}\n\n"
                last_status = status

            # Emit live pass/fail/skip progress (parsed from the full log).
            if log_path and os.path.exists(log_path):
                prog = parse_progress_from_log(log_path)
                yield f"event: progress\ndata: {json.dumps(prog)}\n\n"

            if status in terminal:
                yield f"event: done\ndata: {status}\n\n"
                return

            # Heartbeat / avoid a tight loop. Stop if nothing happens for a long
            # time (safety net against orphaned streams).
            time.sleep(1)
            if idle > 1800:  # ~30 min of no output and not terminal
                yield "event: done\ndata: timeout\n\n"
                return

    return StreamingResponse(_event_gen(), media_type="text/event-stream")


@app.post("/api/builds/{build_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_build(build_id: str):
    store = _load_store()
    b = store["builds"].get(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="build not found")
    if not build_runner.is_running(build_id):
        raise HTTPException(status_code=409, detail="build is not running")

    def _mark(status):
        _upsert_build({"build_id": build_id, "status": status})

    ok = build_runner.cancel_build(build_id, mark_status_cb=_mark)
    if not ok:
        raise HTTPException(status_code=409, detail="could not cancel (already finished?)")
    return {"build_id": build_id, "status": "canceling"}


# ── Image management ──────────────────────────────────────────────────────────
@app.delete("/api/images/{image_id}", dependencies=[Depends(require_auth)])
def delete_image(image_id: str):
    if DRY_RUN:
        return {"ok": True, "detail": f"(dry-run) would delete {image_id}"}
    result = build_runner.delete_image(image_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("detail", "delete failed"))
    # Clear image_id from any build record that referenced it.
    with _store_lock:
        store = _load_store()
        for b in store["builds"].values():
            if b.get("image_id") == image_id:
                b["image_id"] = None
                b["reason"] = (b.get("reason") or "") + " | image deleted"
        _save_store(store)
    return result


# ── Validations (end-to-end: boot image -> health -> OpenSCAP -> teardown) ────
@app.post("/api/validations", dependencies=[Depends(require_auth)])
def submit_validation(req: ValidationRequest):
    image_id = (req.image_id or "").strip()
    if not re.fullmatch(r"img-[0-9a-z]{6,}", image_id):
        raise HTTPException(status_code=400, detail=f"invalid image id: {image_id}")

    # Share the single job lock with builds: one heavy Packer job at a time.
    if not _build_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(f"a build/validation is already running "
                    f"(build_id={_active_build['build_id']}). Wait for it to finish."),
        )

    validation_id = uuid.uuid4().hex[:12]
    _active_build["build_id"] = validation_id
    distro = (req.distro or DEFAULT_DISTRO)

    record = {
        "validation_id": validation_id,
        "image_id": image_id,
        "label": req.label or image_id,
        "distro": distro,
        "status": "queued",
        "created_at": _utcnow(),
        "started_at": None,
        "finished_at": None,
        "health": None,
        "audit": None,
        "reason": None,
        "error": None,
    }
    _upsert_validation(record)

    def _worker():
        try:
            validation_runner.run_validation(
                validation_id,
                image_id,
                on_update=lambda r: _upsert_validation(r),
                dry_run=DRY_RUN,
                label=req.label or "",
                distro=distro,
            )
        finally:
            _active_build["build_id"] = None
            _build_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return {"validation_id": validation_id, "status": "queued", "image_id": image_id,
            "dry_run": DRY_RUN}


@app.get("/api/validations", dependencies=[Depends(require_auth)])
def list_validations():
    data = _load_validations()
    vals = sorted(
        data["validations"].values(),
        key=lambda v: v.get("created_at", ""),
        reverse=True,
    )
    return {"validations": vals}


@app.get("/api/validations/{validation_id}", dependencies=[Depends(require_auth)])
def get_validation(validation_id: str):
    v = _load_validations()["validations"].get(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")
    return v


@app.get("/api/validations/{validation_id}/log", response_class=PlainTextResponse,
         dependencies=[Depends(require_auth)])
def get_validation_log(validation_id: str, tail: int = 400):
    v = _load_validations()["validations"].get(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")
    lp = v.get("log_path")
    if not lp or not os.path.exists(lp):
        return ""
    with open(lp, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-tail:])


@app.get("/api/validations/{validation_id}/stream")
def stream_validation(validation_id: str, token: str = Query(default="")):
    """SSE stream of a validation's log + status (same pattern as builds)."""
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="missing or invalid auth token")
    v = _load_validations()["validations"].get(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")

    terminal = {"succeeded", "failed", "canceled"}
    TAIL_BYTES = 64 * 1024

    def _event_gen():
        pos = 0
        lp = v.get("log_path")
        if lp and os.path.exists(lp):
            pos = max(0, os.path.getsize(lp) - TAIL_BYTES)
        last_status = None
        idle = 0
        while True:
            rec = _load_validations()["validations"].get(validation_id, {})
            status = rec.get("status")
            log_path = rec.get("log_path")
            if log_path and os.path.exists(log_path):
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                if chunk:
                    for line in chunk.splitlines():
                        yield f"data: {line}\n\n"
                    idle = 0
                else:
                    idle += 1
            else:
                idle += 1

            if status != last_status:
                yield f"event: status\ndata: {status}\n\n"
                last_status = status

            if status in terminal:
                yield f"event: done\ndata: {status}\n\n"
                return
            time.sleep(1)
            if idle > 1800:
                yield "event: done\ndata: timeout\n\n"
                return

    return StreamingResponse(_event_gen(), media_type="text/event-stream")


@app.get("/api/validations/{validation_id}/audit/report")
def get_validation_report(validation_id: str, token: str = Query(default="")):
    """Redirect to the OpenSCAP HTML report (uploaded to COS during validation)."""
    if AUTH_TOKEN and token and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid auth token")
    v = _load_validations()["validations"].get(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")
    a = v.get("audit") or {}
    url = a.get("report_url")
    if not url:
        raise HTTPException(status_code=404, detail="no report for this validation")
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.post("/api/validations/{validation_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_validation(validation_id: str):
    v = _load_validations()["validations"].get(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")
    if v.get("status") not in ("queued", "preparing", "running", "canceling"):
        raise HTTPException(status_code=409, detail="validation is not running")

    def _mark(status):
        _upsert_validation({"validation_id": validation_id, "status": status})

    validation_runner.cancel_validation(validation_id, mark_status_cb=_mark)
    return {"validation_id": validation_id, "status": "canceling"}


# ── Saved selections ──────────────────────────────────────────────────────────
SELECTIONS_PATH = os.environ.get(
    "CIS_SELECTIONS_PATH", os.path.join(HERE, "saved_selections.json")
)
_sel_lock = threading.Lock()


def _load_selections() -> dict:
    if not os.path.exists(SELECTIONS_PATH):
        return {"selections": {}}
    with open(SELECTIONS_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def _save_selections(data: dict):
    tmp = SELECTIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SELECTIONS_PATH)


class SavedSelection(BaseModel):
    name: str
    selection: Selection


@app.get("/api/selections", dependencies=[Depends(require_auth)])
def list_selections():
    return _load_selections()


@app.post("/api/selections", dependencies=[Depends(require_auth)])
def save_selection(item: SavedSelection):
    name = item.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    with _sel_lock:
        data = _load_selections()
        data["selections"][name] = {
            "selection": item.selection.model_dump(),
            "saved_at": _utcnow(),
        }
        _save_selections(data)
    return {"ok": True, "name": name}


@app.delete("/api/selections/{name}", dependencies=[Depends(require_auth)])
def delete_selection(name: str):
    with _sel_lock:
        data = _load_selections()
        if name not in data["selections"]:
            raise HTTPException(status_code=404, detail="selection not found")
        del data["selections"][name]
        _save_selections(data)
    return {"ok": True, "name": name}


# ── UI ────────────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    idx = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return PlainTextResponse("UI not found. See frontend/index.html", status_code=404)


if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
