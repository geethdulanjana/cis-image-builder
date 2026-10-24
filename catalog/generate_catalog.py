#!/usr/bin/env python3
"""
generate_catalog.py — build a control catalog from a CIS role (RHEL9 or RHEL10).

It parses:
  1. The role defaults -> the list of <prefix>_rule_* toggles and section
     toggles, with their DEFAULT values. (RHEL10 uses defaults/main/main.yml;
     RHEL9 uses defaults/main.yml — both are auto-detected.)
  2. The role tasks/**/*.yml -> human-readable control titles from `name:`
     fields (e.g. "3.3.1.1 | PATCH | Ensure net.ipv4.ip_forward is configured").
  3. A tke_risk JSON overlay (per distro).

Output: a catalog JSON the UI and vars generator use.

Usage:
    # Generate both distro catalogs (default when run with no args):
    python generate_catalog.py --all

    # Or one distro explicitly:
    python generate_catalog.py --distro rhel10
    python generate_catalog.py --distro rhel9

Defaults assume this script runs from cis-image-builder/catalog/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "rhel-cis-official"))

# Per-distro configuration: role dir, variable prefix, benchmark label,
# tke_risk overlay file, and output catalog file.
DISTROS = {
    "rhel10": {
        "role_dir": os.path.join(ROLES_ROOT, "RHEL10-CIS"),
        "prefix": "rhel10cis",
        "benchmark": "RHEL10-CIS",
        "tke_risk": os.path.join(HERE, "tke_risk.rhel10.json"),
        "out": os.path.join(HERE, "catalog-rhel10.json"),
        "details_out": os.path.join(HERE, "control-details-rhel10.json"),
    },
    "rhel9": {
        "role_dir": os.path.join(ROLES_ROOT, "RHEL9-CIS"),
        "prefix": "rhel9cis",
        "benchmark": "RHEL9-CIS",
        "tke_risk": os.path.join(HERE, "tke_risk.rhel9.json"),
        "out": os.path.join(HERE, "catalog-rhel9.json"),
        "details_out": os.path.join(HERE, "control-details-rhel9.json"),
    },
}

TASK_NAME_RE = re.compile(
    r'name:\s*"?([0-9]+(?:\.[0-9]+)+)\s*\|\s*[A-Z]+\s*\|\s*([^"\n]+)"?'
)

SECTION_NAMES = {
    "1": "Initial Setup (filesystem, SELinux, bootloader, crypto, banners)",
    "2": "Services",
    "3": "Network Configuration",
    "4": "Host Based Firewall",
    "5": "Access, Authentication and Authorization",
    "6": "Logging and Auditing",
    "7": "System Maintenance (file permissions, users)",
}


def find_defaults_files(role_dir: str):
    """RHEL10 keeps split defaults in defaults/main/*.yml; RHEL9 uses a single
    defaults/main.yml. Return the list of defaults files to scan."""
    main_dir = os.path.join(role_dir, "defaults", "main")
    single = os.path.join(role_dir, "defaults", "main.yml")
    files = []
    if os.path.isdir(main_dir):
        for fn in sorted(os.listdir(main_dir)):
            if fn.endswith((".yml", ".yaml")):
                files.append(os.path.join(main_dir, fn))
    elif os.path.isfile(single):
        files.append(single)
    return files


def parse_defaults(defaults_files, prefix: str):
    """Return (rules, sections) scanning all defaults files for the given prefix.

    rules:    { control_id: {"var": var_name, "default": bool} }
    sections: { section_num: {"var": var_name, "default": bool} }
    """
    rule_re = re.compile(
        rf"^\s*({prefix}_rule_([0-9_]+))\s*:\s*(true|false)\s*$", re.I
    )
    section_re = re.compile(
        rf"^\s*({prefix}_section([0-9]+))\s*:\s*(true|false)\s*$", re.I
    )
    rules, sections = {}, {}
    for path in defaults_files:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = rule_re.match(line)
                    if m:
                        var_name = m.group(1)
                        cid = var_name[len(prefix) + len("_rule_"):].replace("_", ".")
                        rules[cid] = {"var": var_name,
                                      "default": m.group(3).lower() == "true"}
                        continue
                    m = section_re.match(line)
                    if m:
                        sections[m.group(2)] = {
                            "var": m.group(1),
                            "default": m.group(3).lower() == "true",
                        }
        except OSError:
            continue
    return rules, sections


def parse_task_titles(tasks_dir: str):
    titles = {}
    for root, _dirs, files in os.walk(tasks_dir):
        for fn in files:
            if not fn.endswith((".yml", ".yaml")):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            for m in TASK_NAME_RE.finditer(text):
                cid, title = m.group(1), m.group(2).strip()
                if cid not in titles or len(title) < len(titles[cid]):
                    titles[cid] = title
    return titles


# Control id that begins a task name, e.g.  - name: "3.3.1 | PATCH | ..."
_TASK_BLOCK_NAME_RE = re.compile(r'^(\s*)-\s*name:\s*"?([0-9]+(?:\.[0-9]+)+)\b')
# Literal shell/command module usage inside a task block.
_CMD_MODULE_RE = re.compile(
    r"(ansible\.builtin\.(?:shell|command)|^\s*(?:shell|command)):", re.M
)
# Simple module reference, e.g. "ansible.builtin.sysctl:" or "ansible.posix.mount:"
_MODULE_RE = re.compile(r"^\s{4,}(ansible\.[a-z]+\.[a-z_]+|[a-z_]+):\s*$", re.M)


def _split_task_blocks(text: str):
    """Yield (control_id, block_text, rel_file) for each top-level task that
    starts with a control id. A block runs from its '- name:' line until the
    next task at the same indent (or EOF)."""
    lines = text.splitlines()
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        m = _TASK_BLOCK_NAME_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        cid = m.group(2)
        start = i
        i += 1
        # Consume until the next line that is a task start ('- ') at the same
        # indent, or a line with LESS indentation that isn't blank.
        while i < n:
            line = lines[i]
            if line.strip() == "":
                i += 1
                continue
            cur_indent = len(line) - len(line.lstrip(" "))
            is_task_start = line.lstrip(" ").startswith("- ")
            if cur_indent < indent:
                break
            if cur_indent == indent and is_task_start:
                break
            i += 1
        blocks.append((cid, "\n".join(lines[start:i])))
    return blocks


def parse_task_details(tasks_dir: str, role_dir: str):
    """Return { control_id: {tasks_yaml, commands, modules, files} }.

    tasks_yaml: the concatenated raw task block(s) implementing the control.
    commands:   literal shell/command snippets found in those blocks.
    modules:    ansible modules referenced (sysctl, lineinfile, mount, ...).
    files:      source file(s) the blocks came from (relative to the role).
    """
    details = {}
    for root, _dirs, files in os.walk(tasks_dir):
        for fn in sorted(files):
            if not fn.endswith((".yml", ".yaml")):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, role_dir).replace("\\", "/")
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            for cid, block in _split_task_blocks(text):
                d = details.setdefault(
                    cid, {"tasks_yaml": "", "commands": [], "modules": set(), "files": set()}
                )
                d["tasks_yaml"] += (("\n\n" if d["tasks_yaml"] else "") + block)
                d["files"].add(rel)
                for mm in _MODULE_RE.finditer(block):
                    mod = mm.group(1)
                    if mod not in ("name", "when", "tags", "block", "vars",
                                   "notify", "become", "register", "loop"):
                        d["modules"].add(mod)
    # normalize sets to sorted lists
    for cid, d in details.items():
        d["modules"] = sorted(d["modules"])
        d["files"] = sorted(d["files"])
        d["commands"] = _extract_commands(d["tasks_yaml"])
    return details


def _extract_commands(block_text: str):
    """Pull literal shell/command invocations out of a task block for display.
    Best-effort: captures the value after a shell/command module key, whether
    inline or a block scalar."""
    cmds = []
    lines = block_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if _CMD_MODULE_RE.search(line):
            # Inline form:  ansible.builtin.command: pvcreate /dev/vdb
            after = line.split(":", 1)[1].strip() if ":" in line else ""
            if after and after not in ("|", ">", "|-", ">-", "{{", ""):
                cmds.append(after)
            else:
                # Block scalar: collect subsequent more-indented lines.
                base = len(line) - len(line.lstrip(" "))
                j = i + 1
                collected = []
                while j < len(lines):
                    l2 = lines[j]
                    if l2.strip() == "":
                        j += 1
                        continue
                    ind = len(l2) - len(l2.lstrip(" "))
                    if ind <= base:
                        break
                    # stop if we hit another module key like args:/register:
                    if re.match(r"^\s+[a-z_]+:\s*$", l2) and ind <= base + 2:
                        break
                    collected.append(l2.strip())
                    j += 1
                if collected:
                    cmds.append("\n".join(collected))
                i = j
                continue
        i += 1
    return cmds


def build_catalog(cfg: dict):
    role_dir = cfg["role_dir"]
    prefix = cfg["prefix"]
    if not os.path.isdir(role_dir):
        sys.exit(f"ERROR: role dir not found: {role_dir}")
    defaults_files = find_defaults_files(role_dir)
    if not defaults_files:
        sys.exit(f"ERROR: no defaults files found under {role_dir}/defaults")
    tasks_dir = os.path.join(role_dir, "tasks")
    if not os.path.isdir(tasks_dir):
        sys.exit(f"ERROR: tasks dir not found: {tasks_dir}")

    rules, sections = parse_defaults(defaults_files, prefix)
    titles = parse_task_titles(tasks_dir)
    task_details = parse_task_details(tasks_dir, role_dir)

    tke = {"controls": {}, "section_levers": {}, "feature_levers": {}}
    if os.path.isfile(cfg["tke_risk"]):
        with open(cfg["tke_risk"], encoding="utf-8") as f:
            tke = json.load(f)
    tke_controls = tke.get("controls", {})

    controls = []
    for cid, info in sorted(rules.items(), key=lambda kv: [int(x) for x in kv[0].split(".")]):
        sec = cid.split(".", 1)[0]
        entry = {
            "id": cid,
            "section": sec,
            "section_name": SECTION_NAMES.get(sec, f"Section {sec}"),
            "var": info["var"],
            "default": info["default"],
            "title": titles.get(cid, ""),
            "level": "level1",
            "tke_risk": "safe",
            "has_detail": cid in task_details,
        }
        risk = tke_controls.get(cid)
        if risk:
            entry["tke_risk"] = risk.get("risk", "safe")
            entry["tke_reason"] = risk.get("reason", "")
            if "lever" in risk:
                entry["tke_lever"] = risk["lever"]
            if "safe_value" in risk:
                entry["tke_safe_value"] = risk["safe_value"]
        controls.append(entry)

    catalog = {
        "benchmark": cfg["benchmark"],
        "prefix": prefix,
        "generated_from": os.path.relpath(role_dir),
        "sections": [
            {
                "number": num,
                "name": SECTION_NAMES.get(num, f"Section {num}"),
                "var": sections.get(num, {}).get("var", f"{prefix}_section{num}"),
                "default": sections.get(num, {}).get("default", True),
                "control_count": sum(1 for c in controls if c["section"] == num),
            }
            for num in sorted(SECTION_NAMES.keys())
        ],
        "controls": controls,
        "tke_section_levers": tke.get("section_levers", {}),
        "tke_feature_levers": tke.get("feature_levers", {}),
        "counts": {
            "total_controls": len(controls),
            "breaking": sum(1 for c in controls if c["tke_risk"] == "breaking"),
            "caution": sum(1 for c in controls if c["tke_risk"] == "caution"),
            "with_title": sum(1 for c in controls if c["title"]),
            "with_detail": sum(1 for c in controls if c["has_detail"]),
        },
    }

    # Build the standalone details map (served on demand by the API).
    details_map = {
        "benchmark": cfg["benchmark"],
        "prefix": prefix,
        "controls": {
            cid: {
                "id": cid,
                "title": titles.get(cid, ""),
                "tasks_yaml": d["tasks_yaml"],
                "commands": d["commands"],
                "modules": d["modules"],
                "files": d["files"],
            }
            for cid, d in task_details.items()
        },
    }
    return catalog, details_map


def generate_one(distro: str):
    cfg = DISTROS[distro]
    catalog, details_map = build_catalog(cfg)
    with open(cfg["out"], "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2)
    with open(cfg["details_out"], "w", encoding="utf-8") as f:
        json.dump(details_map, f, indent=2)
    c = catalog["counts"]
    print(f"[{distro}] wrote {os.path.basename(cfg['out'])} + "
          f"{os.path.basename(cfg['details_out'])}: "
          f"controls={c['total_controls']} with_title={c['with_title']} "
          f"with_detail={c['with_detail']} breaking={c['breaking']} caution={c['caution']}")


def main():
    ap = argparse.ArgumentParser(description="Generate CIS control catalog(s)")
    ap.add_argument("--distro", choices=list(DISTROS), help="single distro")
    ap.add_argument("--all", action="store_true", help="generate all distros (default)")
    args = ap.parse_args()

    if args.distro:
        generate_one(args.distro)
    else:
        for d in DISTROS:
            if os.path.isdir(DISTROS[d]["role_dir"]):
                generate_one(d)
            else:
                print(f"[{d}] skipped (role dir not present: {DISTROS[d]['role_dir']})")


if __name__ == "__main__":
    main()
