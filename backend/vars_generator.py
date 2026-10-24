#!/usr/bin/env python3
"""
vars_generator.py — turn a control selection into a role-compatible group_vars
all.yml for the RHEL9-CIS or RHEL10-CIS role (distro-aware).

A "selection" is a dict:
    {
        "distro":              "rhel10" | "rhel9",   # which CIS role/catalog
        "sections":            {"1": true, ..., "7": true},
        "levels":              {"level_1": true, "level_2": true},
        "disabled_controls":   ["5.1.20", ...],       # rules to turn OFF
        "enabled_controls":    ["..."],                # rules to force ON
        "tke_safe":            true,                   # apply K8s exception set
        "cloud_image_exceptions": true                 # bootloader/repo-gpg/gdm
    }

Or a preset name from presets.json (presets may carry a "distro").

The variable PREFIX differs by distro (rhel9cis_ vs rhel10cis_), but the role
levers for the TKE exceptions are the same shape in both roles:
    <prefix>_is_router: true            # keep ip_forward on (kube-proxy/CNI)
    <prefix>_ipv6_required: true
    <prefix>_firewall: none             # don't let firewalld block kubelet
    <prefix>_rule_1_1_2_4_2: false      # no nodev on /var
    <prefix>_rule_1_1_2_4_3: false      # no nosuid on /var
    <prefix>_selinux_enforce: permissive

The generated file also sets `cis_distro: <distro>` so the playbook can select
the matching role.

Usage (CLI):
    python vars_generator.py --preset tke-safe-full --out all.yml
    python vars_generator.py --selection selection.json --out all.yml
    python vars_generator.py --preset tke-safe-full --distro rhel9
"""
from __future__ import annotations

import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_DIR = os.path.normpath(os.path.join(HERE, "..", "catalog"))
DEFAULT_PRESETS = os.path.join(HERE, "presets.json")

DEFAULT_DISTRO = "rhel10"

# Per-distro config: variable prefix + catalog file.
DISTROS = {
    "rhel10": {"prefix": "rhel10cis", "catalog": os.path.join(CATALOG_DIR, "catalog-rhel10.json")},
    "rhel9":  {"prefix": "rhel9cis",  "catalog": os.path.join(CATALOG_DIR, "catalog-rhel9.json")},
}


def load_json(path):
    # utf-8-sig tolerates an optional BOM (e.g. files written on Windows).
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _distro_cfg(distro: str) -> dict:
    if distro not in DISTROS:
        raise ValueError(f"Unknown distro '{distro}'. Options: {', '.join(DISTROS)}")
    return DISTROS[distro]


def resolve_catalog_path(distro: str) -> str:
    return _distro_cfg(distro)["catalog"]


def resolve_preset(name, presets_path=DEFAULT_PRESETS):
    presets = load_json(presets_path)["presets"]
    if name not in presets:
        raise KeyError(f"Unknown preset '{name}'. Available: {', '.join(presets)}")
    return presets[name]


def tke_exceptions(prefix: str) -> dict:
    return {
        f"{prefix}_is_router": True,
        f"{prefix}_ipv6_required": True,
        # Skip host firewall management (TKE uses the cloud SG). The role guards
        # the firewall package install with `<prefix>_firewall is not none`, i.e.
        # it only skips when the value is YAML null — NOT the string "none".
        # Emitting the string "none" makes the role try `dnf install none`
        # ("No package none available"). So this MUST be None -> rendered as `~`.
        f"{prefix}_firewall": None,
        f"{prefix}_rule_1_1_2_4_2": False,
        f"{prefix}_rule_1_1_2_4_3": False,
        f"{prefix}_selinux_disable": False,
        f"{prefix}_selinux_enforce": "permissive",
        f"{prefix}_selinux_pol": "targeted",
    }


def cloud_image_exceptions(prefix: str) -> dict:
    return {
        f"{prefix}_set_boot_pass": False,
        f"{prefix}_rule_1_4_1": False,
        f"{prefix}_rule_1_4_2": False,
        f"{prefix}_rhel_default_repo": True,
        f"{prefix}_enable_repogpg": False,
        f"{prefix}_desktop_required": False,
    }


def section5_authselect(prefix: str) -> dict:
    return {
        f"{prefix}_allow_authselect_updates": True,
        f"{prefix}_authselect_custom_profile_name": "cis_tke_hardened",
        f"{prefix}_sshd_allowusers": "",
        f"{prefix}_sshd_allowgroups": "",
    }


def build_vars(selection: dict, distro: str) -> dict:
    """Return an ordered dict of role variables for the given selection+distro."""
    cfg = _distro_cfg(distro)
    prefix = cfg["prefix"]
    catalog = load_json(cfg["catalog"])
    control_ids = {c["id"] for c in catalog["controls"]}

    sections = selection.get("sections") or {str(i): True for i in range(1, 8)}
    levels = selection.get("levels") or {"level_1": True, "level_2": True}
    disabled = set(selection.get("disabled_controls") or [])
    enabled = set(selection.get("enabled_controls") or [])
    tke_safe = selection.get("tke_safe", True)
    cloud_exc = selection.get("cloud_image_exceptions", True)

    # Validate control ids against THIS distro's catalog (fail loud on typos /
    # cross-distro ids like a rhel10 3.3.1.1 submitted for rhel9).
    for cid in disabled | enabled:
        if cid not in control_ids:
            raise ValueError(f"Control '{cid}' not in {distro} catalog")

    v = {}

    # Tell the playbook which role to run.
    v["cis_distro"] = distro

    # General build settings.
    v["os_check"] = True
    v[f"{prefix}_uses_root"] = True
    # skip_reboot=TRUE: the role's bare `ansible.builtin.reboot:` task is fragile
    # on Tencent — the instance's network takes a moment to return and Ansible's
    # reboot reconnect hits a fatal `dial tcp ...:22: i/o timeout`. Instead we let
    # the role SKIP its reboot, and the Packer BUILD TEMPLATE does the reboot
    # itself (expect_disconnect + pause_before) and runs `augenrules --load` so
    # the audit rules become active — Packer's reboot handling is robust where
    # the role's is not. Overridable per selection if ever needed.
    v["skip_reboot"] = bool(selection.get("skip_reboot", True))
    v["run_audit"] = selection.get("run_audit", False)

    # Section toggles.
    for num in sorted(sections):
        v[f"{prefix}_section{num}"] = bool(sections[num])

    # Levels.
    v[f"{prefix}_level_1"] = bool(levels.get("level_1", True))
    v[f"{prefix}_level_2"] = bool(levels.get("level_2", True))

    # Per-control overrides (only non-default toggles, to keep the file lean).
    for cid in sorted(disabled, key=_cid_key):
        v[_cid_to_var(prefix, cid)] = False
    for cid in sorted(enabled, key=_cid_key):
        v[_cid_to_var(prefix, cid)] = True

    # Authselect block runs on every play (tags: always) gated only by
    # <prefix>_allow_authselect_updates (role default TRUE). Set it in both
    # directions so a non-section-5 build doesn't trip the profile-name assert.
    if sections.get("5", False):
        v.update(section5_authselect(prefix))
    else:
        v[f"{prefix}_allow_authselect_updates"] = False

    # Cloud-image exceptions.
    if cloud_exc:
        v.update(cloud_image_exceptions(prefix))

    # TKE exceptions LAST so they always win.
    if tke_safe:
        v.update(tke_exceptions(prefix))

    # BUILD-SAFETY (applied last, always): defer disabling root SSH login
    # (control 5.1.20) during the build. The ansible-lockdown role disables
    # PermitRootLogin in Section 5, BEFORE the post-play reboot. Packer connects
    # as root, so after the reboot it can no longer reconnect ("unable to
    # authenticate") and the build fails. We skip 5.1.20 here and the Packer
    # template sets `PermitRootLogin no` as the final step before snapshot, so
    # the shipped image is still compliant while the build survives the reboot.
    v[f"{prefix}_rule_5_1_20"] = False

    return v


def _cid_key(cid: str):
    return [int(x) for x in cid.split(".")]


def _cid_to_var(prefix: str, cid: str) -> str:
    return f"{prefix}_rule_" + cid.replace(".", "_")


def _yaml_scalar(val) -> str:
    if val is None:
        return "~"  # YAML null — role guards use `is not none` to skip tasks
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        return '""' if val == "" else val
    return str(val)


def render_yaml(v: dict, selection_label: str = "", distro: str = "") -> str:
    lines = [
        "---",
        "# ============================================================",
        "# GENERATED by cis-image-builder/backend/vars_generator.py",
        "# Do not hand-edit; regenerate from the UI selection instead.",
    ]
    if distro:
        lines.append(f"# Distro: {distro}")
    if selection_label:
        lines.append(f"# Selection: {selection_label}")
    lines += [
        "# ============================================================",
        "",
    ]
    for key, val in v.items():
        lines.append(f"{key}: {_yaml_scalar(val)}")
    lines.append("")
    return "\n".join(lines)


def generate(selection=None, preset=None, distro=None, presets_path=DEFAULT_PRESETS,
             catalog_path=None):
    """High-level entry: resolve preset or selection -> YAML string.

    `distro` precedence: explicit arg > selection["distro"] > preset["distro"]
    > DEFAULT_DISTRO. `catalog_path` is accepted for backward-compat but ignored
    in favour of the per-distro catalog.
    """
    label = ""
    preset_distro = None
    if preset:
        p = resolve_preset(preset, presets_path)
        label = p.get("label", preset)
        preset_distro = p.get("distro")
        selection = {
            "sections": p.get("sections", {}),
            "levels": p.get("levels", {}),
            "disabled_controls": p.get("disabled_controls", []),
            "enabled_controls": p.get("enabled_controls", []),
            "tke_safe": p.get("tke_safe", True),
            "cloud_image_exceptions": p.get("cloud_image_exceptions", True),
            "run_audit": p.get("run_audit", False),
            "distro": p.get("distro"),
        }
    if selection is None:
        raise ValueError("Provide either a preset name or a selection dict")

    resolved = (distro or selection.get("distro") or preset_distro or DEFAULT_DISTRO)
    v = build_vars(selection, resolved)
    return render_yaml(v, label, resolved)


def main():
    ap = argparse.ArgumentParser(description="Generate group_vars all.yml from a selection/preset")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preset", help="Preset name from presets.json")
    g.add_argument("--selection", help="Path to a selection JSON file")
    ap.add_argument("--distro", choices=list(DISTROS), help="override distro")
    ap.add_argument("--presets", default=DEFAULT_PRESETS)
    ap.add_argument("--out", help="Output path (default: stdout)")
    args = ap.parse_args()

    selection = load_json(args.selection) if args.selection else None
    yaml_text = generate(
        selection=selection,
        preset=args.preset,
        distro=args.distro,
        presets_path=args.presets,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        print(f"Wrote {args.out}")
    else:
        print(yaml_text)


if __name__ == "__main__":
    main()
