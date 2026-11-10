#!/usr/bin/env python3
"""Fail when a PR introduces a known vulnerability in a lockfile.

Scans every lockfile the PR changes with osv-scanner, at the PR base and at the PR head, and reports only the
vulnerabilities that are new at the head. Fails on HIGH and CRITICAL; lower severities are warnings.
Usage (inside a checkout of the PR head, with the base branch fetched): deps.py --osv <osv-scanner> --base origin/main
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "poetry.lock",
             "uv.lock", "Pipfile.lock", "Gemfile.lock", "composer.lock", "go.mod"}
BLOCKING = {"HIGH", "CRITICAL"}
SCORE_LABEL = ((9.0, "CRITICAL"), (7.0, "HIGH"), (4.0, "MODERATE"), (0.0, "LOW"))


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def scan(osv, path):
    out = subprocess.run([osv, "scan", "-L", path, "--format", "json"], capture_output=True, text=True)
    if out.returncode not in (0, 1, 128):
        sys.exit(f"::warning::osv-scanner failed on {path} (exit {out.returncode}); dependency check skipped: "
                 f"{out.stderr.strip()[-300:]}")
    found = {}
    for result in json.loads(out.stdout or "{}").get("results") or []:
        for pkg in result.get("packages") or []:
            scores = {g["ids"][0]: float(g.get("max_severity") or 0) for g in pkg.get("groups") or [] if g.get("ids")}
            for vuln in pkg.get("vulnerabilities") or []:
                score = scores.get(vuln["id"], 0)
                label = (vuln.get("database_specific") or {}).get("severity") or next(
                    l for floor, l in SCORE_LABEL if score >= floor)
                found[(pkg["package"]["name"], vuln["id"])] = (pkg["package"]["version"], label.upper(),
                                                               vuln.get("summary") or "")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osv", required=True)
    ap.add_argument("--base", required=True, help="base ref, e.g. origin/main")
    a = ap.parse_args()
    paths = [p for p in git("diff", "--name-only", "--diff-filter=AM", f"{a.base}...HEAD").stdout.splitlines()
             if os.path.basename(p) in LOCKFILES]
    if not paths:
        print("no lockfile changed")
        return
    new, fixed = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, path in enumerate(paths):
            old_path = os.path.join(tmp, str(i), os.path.basename(path))
            os.makedirs(os.path.dirname(old_path))
            old = git("show", f"{a.base}:{path}")
            old_found = {}
            if old.returncode == 0:
                with open(old_path, "w") as fh:
                    fh.write(old.stdout)
                old_found = scan(a.osv, old_path)
            now = scan(a.osv, path)
            new += [(path, name, *now[(name, vid)], vid) for name, vid in now if (name, vid) not in old_found]
            fixed += len(set(old_found) - set(now))
            print(f"{path}: {len(now)} known vulnerabilities at head, {len(old_found)} at base")
    grouped = {}
    for path, name, version, label, summary, vid in new:
        grouped.setdefault((path, name, version), []).append((label, vid, summary))
    rank = ["CRITICAL", "HIGH", "MODERATE", "LOW"]
    for (path, name, version), vulns in sorted(grouped.items(), key=lambda g: (min(rank.index(v[0]) for v in g[1]
                                                                                if v[0] in rank), g[0])):
        worst = min((v[0] for v in vulns if v[0] in rank), key=rank.index, default="UNKNOWN")
        level = "error" if worst in BLOCKING else "warning"
        ids = ", ".join(v[1] for v in sorted(vulns, key=lambda v: rank.index(v[0]) if v[0] in rank else 9)[:3])
        print(f"::{level} file={path}::{name}@{version} adds {len(vulns)} known vulnerabilities, worst {worst} "
              f"({ids}{', ...' if len(vulns) > 3 else ''})")
    print(f"{len(new)} new, {fixed} fixed by this PR")
    if any(v[0] in BLOCKING for vs in grouped.values() for v in vs):
        sys.exit(1)


if __name__ == "__main__":
    main()
