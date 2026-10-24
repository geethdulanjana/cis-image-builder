#!/usr/bin/env python3
"""
cleanup.py — retention cleanup for the CIS Image Builder.

Prunes:
  1. Old build log files in CIS_BUILD_LOG_DIR older than --log-days (default 30).
  2. Old records in the builds store, keeping the newest --keep-builds
     (default 200). Log files referenced by pruned records are also removed.

Safe to run repeatedly (idempotent). Intended to run from cron/systemd-timer.

Usage:
    python3 cleanup.py                 # use defaults + env
    python3 cleanup.py --log-days 14 --keep-builds 100
    python3 cleanup.py --dry-run       # show what would be removed

Env (shared with the backend):
    CIS_BUILD_LOG_DIR   default /opt/cis-pipeline/logs
    CIS_STORE_PATH      default <backend>/builds_store.json
"""
from __future__ import annotations

import argparse
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STORE = os.path.normpath(os.path.join(HERE, "..", "backend", "builds_store.json"))

LOG_DIR = os.environ.get("CIS_BUILD_LOG_DIR", "/opt/cis-pipeline/logs")
STORE_PATH = os.environ.get("CIS_STORE_PATH", DEFAULT_STORE)


def prune_logs(log_dir: str, days: int, dry_run: bool) -> int:
    if not os.path.isdir(log_dir):
        print(f"log dir not found: {log_dir} (skipping)")
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for fn in os.listdir(log_dir):
        if not fn.startswith("build-") or not fn.endswith(".log"):
            continue
        path = os.path.join(log_dir, fn)
        try:
            if os.path.getmtime(path) < cutoff:
                print(f"{'[dry-run] ' if dry_run else ''}remove old log: {fn}")
                if not dry_run:
                    os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def prune_store(store_path: str, keep: int, dry_run: bool) -> int:
    if not os.path.exists(store_path):
        print(f"store not found: {store_path} (skipping)")
        return 0
    with open(store_path, encoding="utf-8-sig") as f:
        store = json.load(f)
    builds = store.get("builds", {})
    ordered = sorted(builds.values(), key=lambda b: b.get("created_at", ""), reverse=True)
    if len(ordered) <= keep:
        print(f"store has {len(ordered)} builds (<= keep={keep}); nothing to prune")
        return 0
    to_remove = ordered[keep:]
    for b in to_remove:
        bid = b.get("build_id")
        logp = b.get("log_path")
        print(f"{'[dry-run] ' if dry_run else ''}remove build record: {bid}")
        if not dry_run:
            builds.pop(bid, None)
            if logp and os.path.exists(logp):
                try:
                    os.remove(logp)
                except OSError:
                    pass
    if not dry_run:
        tmp = store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
        os.replace(tmp, store_path)
    return len(to_remove)


def main():
    ap = argparse.ArgumentParser(description="CIS Image Builder retention cleanup")
    ap.add_argument("--log-days", type=int, default=30, help="remove build logs older than N days")
    ap.add_argument("--keep-builds", type=int, default=200, help="keep newest N build records")
    ap.add_argument("--log-dir", default=LOG_DIR)
    ap.add_argument("--store", default=STORE_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"log_dir={args.log_dir}  store={args.store}  "
          f"log_days={args.log_days}  keep_builds={args.keep_builds}  dry_run={args.dry_run}")
    n_logs = prune_logs(args.log_dir, args.log_days, args.dry_run)
    n_recs = prune_store(args.store, args.keep_builds, args.dry_run)
    print(f"done: pruned {n_logs} log file(s), {n_recs} build record(s)")


if __name__ == "__main__":
    main()
