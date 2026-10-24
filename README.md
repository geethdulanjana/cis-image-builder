# CIS Image Builder

A web-driven tool to build CIS-hardened RHEL images for Tencent Cloud TKE
(Kubernetes) worker nodes. Choose the **distribution** (RHEL 9 or RHEL 10) and a
**base image**, pick which CIS controls to apply through a UI, and the tool
generates the Ansible variables, runs the Packer build with the matching CIS
role, and returns a custom image ID.

This project is a front-end and orchestration layer over the existing hardening
pipeline in `../rhel-cis-official` (Packer + `ansible-lockdown/RHEL9-CIS` and
`ansible-lockdown/RHEL10-CIS`).

---

## Why

Today, choosing which CIS controls to apply means hand-editing
`ansible/group_vars/all.yml` and knowing which controls break Kubernetes nodes.
That knowledge is tribal and error-prone. This tool turns it into:

- a **control catalog** (machine-readable list of every CIS control),
- **TKE-safety metadata** on each control (does it break a Kubernetes node?),
- **presets** for common scenarios, and
- a **UI** to select controls and trigger a build.

The goal: anyone can produce a correct, node-safe hardened image without editing
YAML or memorizing which sysctl breaks kube-proxy.

---

## Architecture

```
┌──────────────┐      ┌────────────────┐      ┌──────────────────────┐
│    Web UI    │────▶ │  Backend API   │────▶ │   Build runner       │
│ checkboxes   │      │  + build state │      │ (Packer + Ansible on │
│ + presets    │◀──── │                │◀──── │  the builder CVM)    │
└──────────────┘      └────────────────┘      └──────────────────────┘
                            │                          │
                      build history            custom image ID +
                      (control set →            Goss audit result
                       image + score)
```

### Data flow

1. UI loads the per-distro catalog (`catalog/catalog-<distro>.json`) and renders
   controls grouped by section, with TKE-risk badges and presets.
2. User picks a distro + base image, selects controls (or a preset), and submits.
3. Backend renders a role-compatible `group_vars/all.yml` for that distro
   (correct variable prefix + `cis_distro`) — see `backend/vars_generator.py`.
4. Backend runs Packer on the builder CVM, passing the base image as
   `-var source_image_id=<id>`; the playbook selects the RHEL9 or RHEL10 role
   from `cis_distro`.
5. Build produces a custom image; the CIS role's Goss audit can produce a score.
6. Backend stores `{distro, base_image, selection, image_id}` in build history.

---

## The TKE safety model (the important part)

Enabling every CIS control blindly will break a Kubernetes node. This tool
encodes the known Kubernetes-breaking controls as `tke_risk` metadata so the UI
can warn or lock them. The validated exception set (from analysing the
RHEL10-CIS role tasks):

| Concern | Control(s) | Why it breaks TKE | Safe setting |
|---|---|---|---|
| Packet forwarding | `3.3.1.1/2/3`, `3.3.2.1/2` | kube-proxy + CNI need `ip_forward=1` | keep forwarding on via `rhel10cis_is_router: true` |
| Host firewall | Section 4 (`4.1.x`) | firewalld blocks kubelet(10250)/NodePort/CNI | `rhel10cis_firewall: none` (cloud SG is the boundary) |
| /var mounts | `1.1.2.4.2`, `1.1.2.4.3` | kubelet/containerd live under `/var/lib` | no nodev/nosuid on `/var` |
| SELinux | `1.3.x` | containerd contexts unvalidated | `permissive` until validated |

Controls NOT in this list are safe to enable for a worker node. The role does
**not** blacklist `br_netfilter`/`overlay`, nor remove `conntrack`/`socat`/
`ipset`/`iptables`, nor restrict user namespaces — so Sections 2, 5, 6, 7 and
process hardening (1.5) are safe to fully enable.

The table above shows the RHEL 10 control numbering. **RHEL 9** uses the same
TKE levers (`rhel9cis_is_router`, `rhel9cis_firewall`, `rhel9cis_selinux_enforce`)
but different control ids — notably IP forwarding is a single control `3.3.1`
(vs RHEL 10's `3.3.1.1/2/3`). Each distro has its own curated `tke_risk` overlay.

---

## Multi-distro (RHEL 9 and RHEL 10)

The tool supports both benchmarks. The distro is chosen in the UI (or via the
API) and drives three things:

| Aspect | RHEL 9 | RHEL 10 |
|---|---|---|
| CIS role | `../rhel-cis-official/RHEL9-CIS` | `../rhel-cis-official/RHEL10-CIS` |
| Variable prefix | `rhel9cis_` | `rhel10cis_` |
| Catalog | `catalog/catalog-rhel9.json` | `catalog/catalog-rhel10.json` |
| Defaults layout | single `defaults/main.yml` | split `defaults/main/*.yml` |
| ip_forward control | `3.3.1` | `3.3.1.1` / `3.3.1.2` / `3.3.1.3` |

How it fits together:

- `catalog/generate_catalog.py` generates one catalog per distro (auto-detects
  the defaults layout and variable prefix).
- `backend/vars_generator.py` emits the correct prefix and writes `cis_distro`
  into `group_vars/all.yml`. It validates selected control ids against *that
  distro's* catalog, so a RHEL 10 id can't be submitted for a RHEL 9 build.
- `rhel-cis-official/ansible/playbook.yml` reads `cis_distro` and includes the
  matching role (`RHEL9-CIS` or `RHEL10-CIS`), asserting the value is known.
- The **base image** is selected per build and passed to Packer as
  `-var source_image_id=<img-xxxx>`; blank uses the template default.

Presets are **distro-agnostic** (they carry no distro-specific control ids), so
the same preset works for either distro — the distro is selected separately.

---

## Repository layout

```
cis-image-builder/
├── README.md                 ← this file (updated as the project evolves)
├── catalog/
│   ├── generate_catalog.py    ← parses each CIS role's defaults → catalog-<distro>.json
│   ├── catalog-rhel10.json    ← generated RHEL10 control catalog (do not hand-edit)
│   ├── catalog-rhel9.json     ← generated RHEL9 control catalog (do not hand-edit)
│   ├── tke_risk.rhel10.json   ← curated RHEL10 TKE-risk overrides
│   └── tke_risk.rhel9.json    ← curated RHEL9 TKE-risk overrides
├── backend/
│   ├── app.py                 ← FastAPI backend + serves the UI
│   ├── build_runner.py        ← runs Packer on the builder CVM, tracks status
│   ├── vars_generator.py      ← selection → group_vars/all.yml
│   ├── presets.json           ← TKE-Safe Full / Level 1 / Level 2 / Custom
│   ├── requirements.txt
│   └── run.sh                 ← launch script (DRY_RUN=1 for local dev)
├── frontend/
│   └── index.html             ← single-page UI (vanilla JS, no build step)
├── deploy/
│   ├── cis-image-builder.service          ← systemd unit for the backend
│   ├── cleanup.py                         ← log/store retention cleanup
│   ├── cis-image-builder-cleanup.service  ← oneshot cleanup service
│   └── cis-image-builder-cleanup.timer    ← daily cleanup timer
└── docs/
    └── (design notes, added as we go)
```

---

## Roadmap

### Phase 0 — Foundations (complete)
- [x] Project scaffold + README
- [x] Control catalog generator + `catalog.json` (328 controls, TKE-risk flagged)
- [x] Vars-file generator (selection/preset → `all.yml`)
- [x] Presets as data (`presets.json`)

### Phase 1 — MVP (preset-driven) (complete)
- [x] Backend API (submit build, poll status, list history)
- [x] Trigger Packer build on builder CVM (runs packer locally via CAM role)
- [x] Minimal UI: presets + per-control view + build button + status + log
- [x] Store build history (file-backed) — selection, status, image ID
- [ ] Capture audit score into history (Phase 3 / when audit output is parsed)

### Phase 2 — Custom control selection (complete)
- [x] Full per-control UI with section grouping
- [x] TKE-risk warnings / locks on dangerous controls
- [x] Diff view: "what changed vs. a preset"
- [x] Save / load / delete named custom selections
- [x] Human-readable build outcome ("reason") in history
- [x] Cancel a running build
- [x] Delete a created Tencent image from the UI

### Phase 3 — Hardening & ops (in progress)
- [x] Auth (shared token on all `/api` requests)
- [x] One-build-at-a-time lock
- [x] systemd service + daily retention cleanup (logs + store)
- [ ] RBAC (per-user roles, approval for risky controls)
- [ ] Cost controls, build timeouts
- [ ] Audit report rendering in the UI (blocked on successful builds / license)

### Phase 4 — Multi-distro + base-image selection (complete)
- [x] Per-distro catalogs (RHEL 9 + RHEL 10) with distro-specific TKE risk
- [x] Distro-aware vars generator (correct prefix + `cis_distro`)
- [x] Playbook selects the RHEL9 or RHEL10 role from `cis_distro`
- [x] Base image id passed to Packer per build (`-var source_image_id`)
- [x] UI distro selector + base image field; loads the matching catalog
- [ ] Auto-detect distro from a chosen base image (future convenience)

---

## Design decisions

- **Preset-first, not free-for-all.** The MVP ships curated presets. Free
  per-control selection comes later, behind TKE-risk guardrails. This avoids
  producing images that fail to boot or fail to join the cluster.
- **Catalog is generated, not hand-maintained.** `catalog.json` is derived from
  the role defaults so it stays in sync when the role is updated. TKE-risk flags
  are layered on via `tke_risk.json`.
- **The tool never hand-edits the role.** It only generates a `group_vars`
  overlay, exactly like the manual workflow does today.
- **Secrets stay out.** Tencent credentials come from the builder's CAM role;
  the UI/backend never handle raw keys.

---

## Usage (catalog + vars generator)

Regenerate the control catalogs from the roles (run after a role is updated).
With no args it generates every distro whose role is present:

```bash
cd catalog
python generate_catalog.py            # -> catalog-rhel10.json + catalog-rhel9.json
python generate_catalog.py --distro rhel9   # one distro
```

Generate a role-compatible `all.yml` from a preset, choosing the distro:

```bash
cd backend
python vars_generator.py --preset tke-safe-full --distro rhel10 --out all.yml
python vars_generator.py --preset tke-safe-full --distro rhel9  --out all.yml
```

Or from a custom selection file (the selection may carry `"distro"`):

```bash
python vars_generator.py --selection my-selection.json --out all.yml
```

A selection looks like:

```json
{
  "distro": "rhel10",
  "sections": {"1": true, "2": true, "3": true, "4": true, "5": true, "6": true, "7": true},
  "levels": {"level_1": true, "level_2": false},
  "disabled_controls": ["5.1.20"],
  "tke_safe": true,
  "cloud_image_exceptions": true
}
```

`distro` selects the benchmark/role (`rhel10` default, or `rhel9`). Control ids
in `disabled_controls`/`enabled_controls` are validated against that distro's
catalog.

`tke_safe: true` always applies the Kubernetes exception set last, so it wins
over any conflicting per-control choice — you cannot accidentally disable
forwarding or enable firewalld on a node image.

## Running the app (Phase 1)

The backend runs **on the builder CVM** (it invokes Packer locally and uses the
CVM's CAM role for credentials via the metadata service). It also serves the UI.

### Local development (no Packer, simulated builds)

```bash
cd backend
DRY_RUN=1 ./run.sh
# open http://localhost:8080
```

In `DRY_RUN=1`, submitting a build writes the generated `all.yml` and returns a
fake image id (`img-dryrun00`) without touching Packer or Tencent metadata —
useful for exercising the UI and API on any machine.

### On the builder CVM (real builds)

Prefer the systemd service (see **Deployment** below). For a quick foreground
run:

```bash
cd /root/cis-image-builder/backend
CIS_AUTH_TOKEN='a-long-random-secret' ./run.sh
# UI at http://<builder-ip>:8080  (front it with the cloud SG / a bastion)
```

The builder pipeline lives at `/opt/cis-pipeline` (i.e. `packer/`, `ansible/`,
`logs/` directly under it). The runner writes the generated vars to
`CIS_GROUP_VARS_PATH` (default `/opt/cis-pipeline/ansible/group_vars/all.yml`),
then runs `packer build <CIS_PACKER_DIR>` from `CIS_PIPELINE_DIR`, streaming to
`CIS_BUILD_LOG_DIR`. `run.sh` sets these defaults; override via env if needed.

### API reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/catalog?distro=rhel10\|rhel9` | Control catalog for a distro (default rhel10) |
| GET | `/api/distros` | Supported distros + availability + default |
| GET | `/api/presets` | Available presets (distro-agnostic) |
| GET | `/api/status` | Build-running flag, active build id, dry-run/auth state |
| POST | `/api/preview` | Render `all.yml` for `{preset\|selection, distro}` (no build) |
| POST | `/api/builds` | Submit an async build `{preset\|selection, distro, base_image_id?}` |
| GET | `/api/builds` | Build history (newest first, includes `reason`) |
| GET | `/api/builds/{id}` | One build record (includes generated vars) |
| GET | `/api/builds/{id}/log?tail=N` | Tail of the build log |
| POST | `/api/builds/{id}/cancel` | Cancel a running build (terminates Packer) |
| DELETE | `/api/images/{image_id}` | Delete a Tencent custom image by id |
| GET | `/api/selections` | List saved custom selections |
| POST | `/api/selections` | Save a named selection `{name, selection}` |
| DELETE | `/api/selections/{name}` | Delete a saved selection |
| GET | `/` | The UI |

Example — submit a build from a preset:

```bash
curl -X POST localhost:8080/api/builds \
  -H 'Content-Type: application/json' \
  -d '{"preset":"tke-safe-full","distro":"rhel9","base_image_id":"img-xxxxxxxx","label":"nightly"}'
```

### Access control (auth token)

Set `CIS_AUTH_TOKEN` to require a shared token on every `/api` request. When
set, requests must send it as `X-Auth-Token: <token>` (or
`Authorization: Bearer <token>`). If unset, auth is **disabled** and `run.sh`
prints a warning — do not run unauthenticated behind a public CLB.

```bash
CIS_AUTH_TOKEN='a-long-random-secret' ./run.sh
```

The UI prompts for the token on first use (on a 401) and stores it in the
browser's localStorage, sending it on every call.

### One build at a time (build lock)

Only a single build can run at a time. A second submit while one is in progress
returns HTTP 409 and the UI disables the Build button and shows "Build
running…" (driven by `GET /api/status`). This protects the shared
`group_vars/all.yml` file and the single Packer workspace from concurrent
clobbering. `GET /api/status` reports `{build_running, active_build_id}`.

### Safety note

The UI locks **breaking** controls (e.g. IP forwarding) whenever TKE-safe is on,
and the vars generator applies the TKE exception set last regardless of UI
state. You cannot produce a node-breaking image through the normal flow.

## UI features

- **Distro selector + base image field** — choose RHEL 9 or RHEL 10 (loads the
  matching catalog) and the Tencent base image to harden (blank = template
  default).
- **Preset picker** + TKE-safe toggle.
- **Per-control view** grouped by section, with `breaking`/`caution` badges.
  Breaking controls are locked while TKE-safe is on.
- **Preview vars** — see the exact generated `all.yml` before building.
- **Diff vs preset** — shows which sections/controls a custom selection changed
  relative to its base preset.
- **Saved selections** — name, save, load, and delete custom selections.
- **Builds table** — status, human-readable **reason** (e.g. "failed: RHEL
  subscription/repos required"), image id, and actions.
- **Cancel** a running build; **delete** a created Tencent image (with confirm).

## Deployment (ops)

Run the backend as a managed service on the builder CVM. Files are in `deploy/`.

### Backend service

```bash
cp deploy/cis-image-builder.service /etc/systemd/system/
# edit Environment= lines: set CIS_AUTH_TOKEN and confirm the CIS_* paths
systemctl daemon-reload
systemctl enable --now cis-image-builder
systemctl status cis-image-builder
journalctl -u cis-image-builder -f
```

Key `Environment=` settings in the unit:
`CIS_AUTH_TOKEN` (required), `CIS_PIPELINE_DIR`, `CIS_PACKER_DIR`,
`CIS_GROUP_VARS_PATH`, `CIS_BUILD_LOG_DIR`, `TENCENTCLOUD_REGION`, and
optionally `DRY_RUN=1` / `CIS_PACKER_DEBUG=1`.

### Retention cleanup (daily)

Prunes build logs older than 30 days and trims the store to the newest 200
records (both configurable).

```bash
cp deploy/cis-image-builder-cleanup.service /etc/systemd/system/
cp deploy/cis-image-builder-cleanup.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cis-image-builder-cleanup.timer
systemctl list-timers cis-image-builder-cleanup.timer
```

Run it manually / preview:

```bash
python3 deploy/cleanup.py --dry-run
python3 deploy/cleanup.py --log-days 14 --keep-builds 100
```

## Status

Phases 0–2 and Phase 4 complete; Phase 3 in progress. Working end-to-end on the
builder CVM: pick **distro (RHEL 9 / RHEL 10) + base image** → UI selection
(presets or custom) → generated `all.yml` (correct prefix + `cis_distro`) →
Packer build with the matching CIS role → history with readable outcomes,
cancel, and image delete. Secured with a shared auth token and a
one-build-at-a-time lock; runs as a systemd service with daily log/store
cleanup.

Known external blocker: builds cannot fully succeed until the RHEL
subscription/license path is resolved (tracked separately). Audit-score capture
in the UI is deferred until builds complete.
